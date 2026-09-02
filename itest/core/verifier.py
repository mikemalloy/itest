"""The ``itest verify`` engine.

Runs pytest over the file paths the manifest registers, maps each result back
to the integration point it covers via the manifest, and rolls results up to
point-level coverage. Output is available as a human table, JSON, or JUnit XML.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field

from itest.core import environments, planner, points, redact
from itest.core.manifest import Manifest, load_manifest, save_manifest

JUNIT_NAME = "itest-results.xml"
_REPORT_NAME = "_verify_report.json"


class VerifyConfigError(Exception):
    """Raised for usage/config problems (maps to exit code 2)."""


class TestResult(BaseModel):
    canonical: str
    outcome: str  # passed | failed | skipped | error | missing | gated
    point_id: str | None = None
    detail: str = ""


class PointResult(BaseModel):
    id: str
    source: str
    target: str
    attributes: dict = Field(default_factory=dict)
    status: str  # passing | failing | error | stub | gated
    #: The same one-line tag `itest plan` prints, from itest.core.points.
    #: Carried here because a PointResult has no type, and rendering must not
    #: guess at attributes that only one point type has.
    tag: str = ""


class VerifyReport(BaseModel):
    total_points: int = 0
    passing: int = 0
    failing: int = 0
    errored: int = 0
    stubs: int = 0
    orphaned_tests: int = 0
    #: Points whose every live test sits in a tier this environment disallows.
    gated: int = 0
    elapsed_seconds: float = 0.0
    #: The resolved environment, or None on the safe floor. Carried so the
    #: renderer can name it in the [GATED <env>] tag without a second lookup.
    environment: str | None = None
    #: True when a policy exists but nothing is bound — the announced floor.
    on_safe_floor: bool = False
    points: list[PointResult] = Field(default_factory=list)
    tests: list[TestResult] = Field(default_factory=list)
    unregistered: list[str] = Field(default_factory=list)

    @property
    def exit_code(self) -> int:
        # An errored point means the suite could not run, which is a config
        # problem (exit 2) rather than a test result (exit 1).
        if self.errored > 0:
            return 2
        return 1 if self.failing > 0 else 0


def _pytest_installed() -> bool:
    """True when pytest is importable from the interpreter running ITest."""
    return importlib.util.find_spec("pytest") is not None


def _require_pytest() -> None:
    """Fail fast, and legibly, when pytest is missing.

    verify shells out to ``python -m pytest``; without it the subprocess dies
    with an opaque traceback long after the user could have acted on it.
    """
    if _pytest_installed():
        return
    raise VerifyConfigError(
        "pytest is not installed in the environment ITest is running from:\n"
        f"    {sys.executable}\n"
        "`itest verify` runs the generated suite with pytest, so it cannot "
        "work without it. Install it there with:\n"
        f"    {sys.executable} -m pip install pytest"
    )


def _collection_error_for(path: str, collection_errors: dict[str, dict]):
    """Return the collection error covering ``path``, if any."""
    if path in collection_errors:
        return collection_errors[path]
    for nodeid, err in collection_errors.items():
        # A directory-level failure covers every file beneath it.
        if nodeid and path.startswith(nodeid.rstrip("/") + "/"):
            return err
    return None


def _gated_canonicals(
    manifest: Manifest, resolution: environments.Resolution
) -> set[str]:
    """Canonical addresses of live tests the environment disallows by tier."""
    return {
        t.canonical
        for t in manifest.tests
        if t.status != "orphaned" and not t.disabled and not resolution.allows(t.tier)
    }


def _gating_args(manifest: Manifest, gated: set[str]) -> tuple[list[str], set[str]]:
    """pytest flags that keep gated tests out of collection.

    Returns ``(args, ignored_files)``. A file whose every live test is gated is
    ``--ignore``-d, so it is never imported — the strong guarantee for a
    dedicated active-tier suite. A gated test sharing a file with allowed
    siblings can only be ``--deselect``-ed: the module must import for the
    siblings, but the gated test never runs. Both are collection-time, so
    neither is a runtime skip.

    ``ignored_files`` is returned so the caller can drop those paths from the
    explicit pytest targets: an ``--ignore``-d path passed *positionally* would
    still be collected (an explicit argument overrides ``--ignore``), which
    would defeat the never-import guarantee.
    """
    if not gated:
        return [], set()
    live_by_file: dict[str, list[str]] = {}
    for t in manifest.tests:
        if t.status == "orphaned" or t.disabled:
            continue
        live_by_file.setdefault(t.path, []).append(t.canonical)

    args: list[str] = []
    ignored_files: set[str] = set()
    for path, canonicals in sorted(live_by_file.items()):
        gated_here = [c for c in canonicals if c in gated]
        if not gated_here:
            continue
        if len(gated_here) == len(canonicals):
            args.append(f"--ignore={path}")
            ignored_files.add(path)
        else:
            args += [f"--deselect={c}" for c in gated_here]
    return args, ignored_files


def _run_pytest(
    base_dir: Path,
    junit_path: Path | None,
    targets: list[str],
    gating_args: list[str] | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Run pytest over ``targets`` and return (test outcomes, collection errors).

    ``targets`` is the set of distinct file paths the manifest actually
    registers, not a fixed directory: a test registered anywhere (``itest add``
    onto an existing test under ``tests/`` say) must run, and passing a
    directory would sweep up the customer's unrelated tests. With nothing
    registered there is nothing to run, so pytest is not invoked at all — which
    also avoids pytest defaulting to collecting the whole working tree.
    """
    if not targets:
        return {}, {}

    report_file = base_dir / planner.ITEST_DIR / _REPORT_NAME
    report_file.parent.mkdir(parents=True, exist_ok=True)
    if report_file.exists():
        report_file.unlink()

    args = [
        sys.executable,
        "-m",
        "pytest",
        *targets,
        "-q",
        "-p",
        "itest.core._pytest_report",
        "-p",
        "no:cacheprovider",
        # Without this, the first un-importable module aborts collection and
        # nothing else runs — one broken file would blind every other point.
        "--continue-on-collection-errors",
        # Pin rootdir to the project. Otherwise pytest walks up looking for a
        # pytest.ini / pyproject.toml / setup.cfg and, on finding one in an
        # ancestor (a monorepo's terraform/ dir, say), reports node ids as
        # "sub/dir/itest_tests/..." — which never match the manifest's
        # "itest_tests/..." paths, so every passing test looks unregistered.
        f"--rootdir={base_dir}",
    ]
    # Gated tests are removed from collection here, before any import.
    args += gating_args or []
    if junit_path is not None:
        args += ["--junitxml", str(junit_path)]

    env = dict(os.environ, ITEST_REPORT=str(report_file))
    subprocess.run(args, cwd=str(base_dir), env=env, capture_output=True, text=True)

    if not report_file.exists():
        return {}, {}
    document = json.loads(report_file.read_text(encoding="utf-8"))
    return document.get("tests", {}), document.get("collection_errors", {})


def _scrub_report(report: VerifyReport, scrub) -> VerifyReport:
    """Return the report with every string run through ``scrub``.

    Done over the serialized model rather than field by field: any string the
    report carries now or later is covered — the ARN targets, and equally the
    ``detail`` of a failing test (its assertion message and traceback), which is
    exactly where a leaked token or connection string lands. A new field cannot
    quietly reintroduce a leak.
    """
    text = scrub(report.model_dump_json())
    return VerifyReport.model_validate_json(text)


def run_verify(
    base_dir: Path,
    output: str = "human",
    redact_accounts: bool = False,
    environment: str | None = None,
) -> VerifyReport:
    """Execute the suite and build the coverage report.

    ``redact_accounts`` runs document-grade scrubbing over the report and, for
    junit output, over the written XML — account ids and high-entropy tokens /
    credential patterns in every string, including a failing test's detail —
    using one shared mapping so the two still correlate. It does not strip
    human-readable resource names. Verify output gets pasted into tickets and
    CI logs.

    ``environment`` overrides the local binding. The resolved environment's
    tier policy decides which tests are collected at all; a bad policy raises
    ``environments.EnvironmentConfigError`` here, before the suite runs.
    """
    manifest_file = planner.manifest_path(base_dir)
    if not manifest_file.exists():
        raise VerifyConfigError(
            "No manifest found. Run `itest plan && itest sync` first."
        )
    manifest = load_manifest(manifest_file)
    # Resolve (and validate) the policy before requiring pytest or running
    # anything: a policy that would loose a mutating test cannot slip past a
    # green suite, because verify refuses to start.
    resolution = environments.resolve(base_dir, override=environment)
    _require_pytest()

    gated = _gated_canonicals(manifest, resolution)

    gating_args, ignored_files = _gating_args(manifest, gated)
    # The distinct file paths the manifest registers. Orphaned entries name a
    # point that no longer exists, so their file is not a verification target;
    # a fully-gated file is dropped here too, because an explicit positional
    # path would override its --ignore and load the module anyway.
    targets = sorted(
        {
            t.path
            for t in manifest.tests
            if t.status != "orphaned" and t.path not in ignored_files
        }
    )

    junit_path = base_dir / JUNIT_NAME if output == "junit" else None
    started = time.monotonic()
    outcomes, collection_errors = _run_pytest(
        base_dir, junit_path, targets, gating_args
    )
    elapsed = time.monotonic() - started

    by_canonical = {t.canonical: t for t in manifest.tests}

    # Resolve every registered test once. A gated test is neither run nor
    # collected, so it carries its own "gated" outcome rather than looking
    # merely absent. A test in a module that failed to collect inherits its
    # module's error.
    resolved: dict[str, tuple[str, str]] = {}
    durations_recorded = False
    for test in manifest.tests:
        if test.canonical in gated:
            resolved[test.canonical] = ("gated", "")
            continue
        raw = outcomes.get(test.canonical)
        if raw:
            resolved[test.canonical] = (raw["outcome"], raw["detail"])
            if raw.get("duration") is not None:
                test.last_duration_seconds = round(float(raw["duration"]), 6)
                durations_recorded = True
            continue
        err = _collection_error_for(test.path, collection_errors)
        resolved[test.canonical] = ("error", err["detail"]) if err else ("missing", "")

    # Test-level results.
    test_results = [
        TestResult(
            canonical=test.canonical,
            outcome=resolved[test.canonical][0],
            point_id=test.point_id,
            detail=resolved[test.canonical][1],
        )
        for test in manifest.tests
    ]
    unregistered = sorted(n for n in outcomes if n not in by_canonical)

    # Point-level rollup. Gated points are collected apart and appended after
    # the rest, so the Points listing shows them last (after stubs).
    ranked_results: list[PointResult] = []
    gated_results: list[PointResult] = []
    passing = failing = errored = stubs = gated_points = 0
    for point in manifest.points:
        live = [
            t
            for t in manifest.tests_for_point(point.id)
            if t.status != "orphaned" and not t.disabled
        ]
        allowed = [t for t in live if t.canonical not in gated]
        # A point with live coverage, all of it gated, is itself gated: the
        # environment refused every check it has, so it is neither known-good
        # nor merely unimplemented.
        if live and not allowed:
            gated_results.append(
                PointResult(
                    id=point.id,
                    source=point.source,
                    target=point.target,
                    attributes=point.attributes,
                    status="gated",
                    tag=points.summary(point),
                )
            )
            gated_points += 1
            continue

        live_outcomes = [resolved[t.canonical][0] for t in allowed]
        # Precedence: fail > error > pass > stub. A single test that could not
        # run outranks a passing sibling — the point is not known-good, and
        # reporting it as covered is how a broken check goes unnoticed.
        if any(o == "failed" for o in live_outcomes):
            status = "failing"
            failing += 1
        elif any(o == "error" for o in live_outcomes):
            status = "error"
            errored += 1
        elif any(o == "passed" for o in live_outcomes):
            status = "passing"
            passing += 1
        else:
            status = "stub"
            stubs += 1
        ranked_results.append(
            PointResult(
                id=point.id,
                source=point.source,
                target=point.target,
                attributes=point.attributes,
                status=status,
                tag=points.summary(point),
            )
        )

    point_results = ranked_results + gated_results
    orphaned_tests = sum(1 for t in manifest.tests if t.status == "orphaned")

    # Persist per-test durations (schema v2). This is verify's only write to
    # the manifest; test registration and status stay sync's job.
    if durations_recorded:
        save_manifest(manifest, manifest_file)

    report = VerifyReport(
        total_points=len(manifest.points),
        passing=passing,
        failing=failing,
        errored=errored,
        stubs=stubs,
        orphaned_tests=orphaned_tests,
        gated=gated_points,
        elapsed_seconds=round(elapsed, 2),
        environment=resolution.environment,
        on_safe_floor=resolution.on_safe_floor,
        points=point_results,
        tests=test_results,
        unregistered=unregistered,
    )

    if redact_accounts:
        # One scrubber for the whole run, so the report and the junit file agree
        # on which stand-in maps to which real account or token. Document-grade:
        # account ids AND high-entropy tokens / credential patterns in every
        # string, including a failing test's detail. Resource names are left
        # readable — the account-id + token scope `itest redact` documents.
        scrub = redact.text_scrubber()
        report = _scrub_report(report, scrub)
        if junit_path is not None and junit_path.exists():
            junit_path.write_text(
                scrub(junit_path.read_text(encoding="utf-8")), encoding="utf-8"
            )

    return report


_STATUS_TAG = {
    "passing": "PASS",
    "failing": "FAIL",
    "error": "ERROR",
    "stub": "STUB",
}


def _gated_tag(environment: str | None) -> str:
    """The bracketed status a gated point carries. Bare on the safe floor."""
    return f"GATED {environment}" if environment else "GATED"


def render_human(report: VerifyReport, redacted: bool = False) -> str:
    out: list[str] = []
    # One line, only when a policy is committed but nothing is bound: name the
    # floor the run fell back to, so a green suite is not mistaken for coverage
    # of the active tier it silently withheld.
    if report.on_safe_floor:
        out.append(
            "No environment bound: running the safe floor (static, readonly). "
            "Bind one with --environment or .itest/environment."
        )
    rollup = (
        f"{report.total_points} integration points: "
        f"{report.passing} passing, {report.failing} failing, "
        f"{report.errored} errored, "
        f"{report.stubs} stubs, {report.orphaned_tests} orphaned tests"
    )
    # Append-only, like the resurrection clause in plan: the fragment appears
    # only when something is gated, so the common line is byte-identical.
    if report.gated:
        rollup += f", {report.gated} gated"
    out.append(rollup + ".")
    ran = sum(1 for t in report.tests if t.outcome != "gated")
    out.append(f"Ran {ran} tests in {report.elapsed_seconds:.2f}s")
    # A fully-gated point announces itself as [GATED]. A gated test on a
    # point that still ran its other tests has no marker of its own — the
    # point truthfully reports its remaining coverage — so without this line
    # the withholding would be silent, and nothing may be silently skipped.
    # Append-only: the line is absent whenever nothing is partially gated.
    fully_gated_points = {p.id for p in report.points if p.status == "gated"}
    partially_gated = sum(
        1
        for t in report.tests
        if t.outcome == "gated" and t.point_id not in fully_gated_points
    )
    if partially_gated:
        out.append(
            f"{partially_gated} gated test(s) withheld by this environment on "
            "points that still report their remaining tests."
        )
    out.append("")
    out.append("Points:")
    for p in report.points:
        status = (
            _gated_tag(report.environment)
            if p.status == "gated"
            else _STATUS_TAG.get(p.status, "????")
        )
        out.append(f"  [{status}] {p.source} -> {p.target} ({p.tag})")

    failures = [t for t in report.tests if t.outcome == "failed"]
    if failures:
        out.append("")
        out.append("Failing tests:")
        for t in failures:
            out.append(f"  {t.canonical}")
            for line in (t.detail or "").splitlines():
                out.append(f"      {line}")

    errors = [t for t in report.tests if t.outcome == "error"]
    if errors:
        out.append("")
        out.append("Errored tests (the suite could not run):")
        for t in errors:
            out.append(f"  {t.canonical}")
            for line in (t.detail or "").splitlines():
                out.append(f"      {line}")

    if report.unregistered:
        out.append("")
        out.append("Unregistered tests (not in manifest):")
        for n in report.unregistered:
            out.append(f"  {n}")

    # Only worth saying when there is something to leak and it has not been
    # scrubbed already: an ARN target carries an account id. State the scope
    # honestly — --redact is not a blanket "safe to share".
    if not redacted and any(p.target.startswith("arn:") for p in report.points):
        out.append("")
        out.append(
            "Tip: --redact before sharing pseudonymizes account ids and "
            "high-entropy tokens (targets include ARNs); it does not strip "
            "human-readable resource names, which are kept for readability."
        )

    return "\n".join(out)
