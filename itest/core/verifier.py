"""The ``itest verify`` engine.

Runs pytest over the file paths the manifest registers, maps each result back
to the integration point it covers via the manifest, and rolls results up to
point-level coverage. Output is available as a human table, JSON, or JUnit XML.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from itest.core import environments, lifecycle, planner, points, redact, stubgen
from itest.core.manifest import (
    IntegrationPoint,
    Manifest,
    TestEntry,
    load_manifest,
    save_manifest,
)
from itest.traits.ids import LEGACY_BY_TRAIT, trait_ident

JUNIT_NAME = "itest-results.xml"

#: A generous ceiling on the pytest subprocess (seconds). A real integration
#: suite can be slow; this only catches a genuine hang, which would otherwise
#: block the CLI forever.
_PYTEST_TIMEOUT = 1800.0


def _new_report_file(base_dir: Path) -> Path:
    """Create a unique, empty per-run report file under ``.itest/``.

    A fixed name would let two concurrent ``itest verify`` runs read and write
    the same file, so each run gets its own via ``mkstemp``. The file is deleted
    by the caller once read.
    """
    report_dir = base_dir / planner.ITEST_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix="_verify_report_", suffix=".json", dir=report_dir
    )
    os.close(fd)
    return Path(name)


class VerifyConfigError(Exception):
    """Raised for usage/config problems (maps to exit code 2)."""


class TestResult(BaseModel):
    canonical: str
    #: passed | failed | skipped | error | missing | gated | not_applicable
    #: (``not_applicable``: a retired check — its trait no longer applies).
    outcome: str
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
    #: The tool ledger (``{"servers": [...]}``), exactly the shape of
    #: ``tests/fixtures/report/tool-ledger.json``. ``None`` — and absent from
    #: the JSON — when the manifest records no declared tool.
    tools: dict | None = None

    def to_json(self, indent: int | None = 2) -> str:
        """The JSON verify prints: ``tools`` appears only when there is one."""
        exclude = {"tools"} if self.tools is None else None
        return self.model_dump_json(indent=indent, exclude=exclude)

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
        if t.status != "orphaned"
        and not t.disabled
        and not t.retired
        and not resolution.allows(t.tier)
    }


def _disabled_canonicals(manifest: Manifest) -> set[str]:
    """Canonical addresses of disabled (non-orphaned) tests.

    A disabled test is excluded from collection exactly as a gated one is: it is
    skipped in reporting, but skipping it in *reporting* alone still ran it. A
    test disabled because it mutates must not execute.
    """
    return {
        t.canonical for t in manifest.tests if t.disabled and t.status != "orphaned"
    }


def _retired_canonicals(manifest: Manifest) -> set[str]:
    """Canonical addresses of retired checks: kept on disk, never run.

    A retired test's trait no longer applies to its tool. It is removed from
    collection exactly as a disabled one is, and reported as ``not_applicable``.
    """
    return {t.canonical for t in manifest.tests if t.retired and t.status != "orphaned"}


def _gating_args(manifest: Manifest, excluded: set[str]) -> tuple[list[str], set[str]]:
    """pytest flags that keep ``excluded`` tests out of collection.

    ``excluded`` is the union of the tier-gated canonicals and the disabled
    ones: both must never run, and both are removed at collection time rather
    than skipped at runtime. Returns ``(args, ignored_files)``. A file whose
    every non-orphaned test is excluded is ``--ignore``-d, so it is never
    imported — the strong guarantee for a dedicated active-tier suite. An
    excluded test sharing a file with runnable siblings can only be
    ``--deselect``-ed: the module must import for the siblings, but the excluded
    test never runs. Both are collection-time, so neither is a runtime skip.

    ``ignored_files`` is returned so the caller can drop those paths from the
    explicit pytest targets: an ``--ignore``-d path passed *positionally* would
    still be collected (an explicit argument overrides ``--ignore``), which
    would defeat the never-import guarantee.
    """
    if not excluded:
        return [], set()
    # The universe includes disabled tests, so a file of only disabled tests is
    # a fully-excluded file (--ignore'd), and a disabled test beside a runnable
    # sibling is --deselect'd rather than leaving the file collected.
    by_file: dict[str, list[str]] = {}
    for t in manifest.tests:
        if t.status == "orphaned":
            continue
        by_file.setdefault(t.path, []).append(t.canonical)

    args: list[str] = []
    ignored_files: set[str] = set()
    for path, canonicals in sorted(by_file.items()):
        excluded_here = [c for c in canonicals if c in excluded]
        if not excluded_here:
            continue
        if len(excluded_here) == len(canonicals):
            args.append(f"--ignore={path}")
            ignored_files.add(path)
        else:
            args += [f"--deselect={c}" for c in excluded_here]
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

    report_file = _new_report_file(base_dir)

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
    try:
        subprocess.run(
            args,
            cwd=str(base_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=_PYTEST_TIMEOUT,
        )
        # mkstemp created the file empty; the plugin overwrites it. An empty file
        # means the plugin never ran (pytest failed to start), not a real result.
        raw = report_file.read_text(encoding="utf-8")
        if not raw.strip():
            return {}, {}
        document = json.loads(raw)
        return document.get("tests", {}), document.get("collection_errors", {})
    except subprocess.TimeoutExpired as exc:
        raise VerifyConfigError(
            f"pytest did not finish within {_PYTEST_TIMEOUT}s and was killed. "
            "The suite is likely hanging — a probe without a timeout, or a "
            "wedged fixture. Find the slow test, or split the run."
        ) from exc
    finally:
        report_file.unlink(missing_ok=True)


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
    # Gated (tier-disallowed) and disabled tests are both kept out of collection.
    # Gated is tracked separately below because a fully-gated point still
    # reports [GATED]; a disabled test simply does not run.
    retired = _retired_canonicals(manifest)
    excluded = gated | _disabled_canonicals(manifest) | retired

    gating_args, ignored_files = _gating_args(manifest, excluded)
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
        if test.canonical in retired:
            resolved[test.canonical] = ("not_applicable", "")
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
            if t.status != "orphaned" and not t.disabled and not t.retired
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

    tools = build_tool_ledger(
        manifest, base_dir, resolved, outcomes, resolution.environment
    )

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
        tools=tools,
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


# --- the tool ledger ------------------------------------------------------------

#: A pytest outcome, as a tool check's status, when the check recorded no
#: CheckResult of its own. A test that could not run (``error``) is critical: a
#: check that cannot run is not a check, and the page must not read it as fine.
_OUTCOME_STATUS = {
    "passed": "pass",
    "failed": "fail",
    "error": "critical",
    "skipped": "not_run",
    "missing": "not_run",
    "gated": "held_out",
    "not_applicable": "n/a",
}

#: Statuses that mean the check actually ran and said something.
_RAN = ("pass", "fail", "critical", "changed", "not_verifiable")

#: Lifecycle states that count toward VERIFIED (derived in itest.core.lifecycle).
_COUNTED = lifecycle.COUNTED_STATES

_SCHEMA = re.compile(r"schema: (?P<schema>\S+)")


def _docstring_schema(path: Path, test_name: str) -> str | None:
    """The ``schema: <hash>`` a generated check's docstring was frozen with."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    name = test_name.split("[", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            node.name == name
        ):
            match = _SCHEMA.search(ast.get_docstring(node) or "")
            return match["schema"] if match else None
    return None


def check_state(
    *,
    base_dir: Path,
    path: str,
    test_name: str,
    ownership_hash: str,
    point_schema: str | None,
    retired: bool = False,
    orphaned: bool = False,
) -> str:
    """How far one check's test can be trusted as a statement about the tool.

    ``orphan`` and ``not_applicable`` come from the manifest. Otherwise the file
    decides: what it was generated against (the docstring's ``schema:``) and
    whose it is (its content against the recorded ownership hash). ``stale`` is
    the dangerous one — the check was generated against a schema the tool no
    longer has, so nobody has read it against the tool as it is. That holds
    whoever owns the file: a hand-edited one is frozen until a human re-reads
    it, and an ITest-owned one until ``itest sync`` regenerates it (which every
    sync does). Otherwise an owned file is ``current`` and an edited one
    ``hand_edited``; an engine module has no ``schema:`` and reads the manifest
    live. ``recipe_newer`` is not computed: nothing records which recipe version
    a check was generated from.
    """
    if orphaned:
        return "orphan"
    if retired:
        return "not_applicable"
    file = base_dir / path
    exists = file.exists()
    schema = _docstring_schema(file, test_name) if exists else None
    if schema is not None and point_schema is not None and schema != point_schema:
        return "stale"
    if exists and stubgen.file_hash(file) == ownership_hash:
        return "current"
    return "hand_edited"


def _short_detail(outcome: str, detail: str, raw: dict, environment: str | None):
    """One line saying what happened, from pytest's own report."""
    if outcome == "skipped":
        return raw.get("reason") or "skipped"
    if outcome == "missing":
        return "registered, but pytest reported no result for it"
    if outcome == "gated":
        where = environment or "the safe floor"
        return f"withheld: {where} does not run this tier"
    if outcome == "not_applicable":
        return "retired: the trait no longer applies to this tool"
    if outcome == "passed":
        return "passed"
    lines = [line.strip() for line in (detail or "").splitlines() if line.strip()]
    errors = [line[1:].strip() for line in lines if line.startswith("E ")]
    text = (errors or lines or [outcome])[-1]
    return text[:200]


def _tool_from_test_name(entry: TestEntry) -> str:
    """The tool a test covers, read back from its name (for an orphan).

    Three shapes: an engine case (``test_engine[<tool>-<trait>]``), a binding
    (``test_<tool>__<trait ident>``, or ``test_<tool>__B2`` from before the
    rename) and a P30 per-trait stub (``test_a1_<tool>``).
    """
    name = entry.test_name
    if name.startswith(f"{stubgen.ENGINE_TEST}[") and name.endswith("]"):
        return name[len(stubgen.ENGINE_TEST) + 1 : -1].rsplit("-", 1)[0]
    trait = entry.trait or ""
    legacy = LEGACY_BY_TRAIT.get(trait, "")
    for suffix in (f"__{trait_ident(trait)}", f"__{legacy}"):
        if suffix != "__" and name.endswith(suffix):
            return name.removeprefix("test_")[: -len(suffix)]
    if legacy and name.startswith(f"test_{legacy.lower()}_"):
        return name[len(f"test_{legacy.lower()}_") :]
    if "__" in name:
        return name.removeprefix("test_").rsplit("__", 1)[0]
    return f"point {entry.point_id}"


def _pick_entry(entries: list[TestEntry], kind: str) -> TestEntry | None:
    """The test that stands for a check: an engine trait's engine case, a
    generated trait's binding (or its P30 per-trait stub)."""
    engine = [e for e in entries if e.test_name.startswith(f"{stubgen.ENGINE_TEST}[")]
    other = [e for e in entries if e not in engine]
    preferred = (engine or other) if kind == "engine" else (other or engine)
    return preferred[0] if preferred else None


def _tool_checks(
    point: IntegrationPoint,
    manifest: Manifest,
    base_dir: Path,
    table,
    resolved: dict[str, tuple[str, str]],
    outcomes: dict[str, dict],
    environment: str | None,
) -> tuple[list[dict], list[dict]]:
    """The ledger rows for one tool, and the exceptions they raise."""
    entries = [
        t
        for t in manifest.tests
        if t.point_id == point.id and t.trait and t.status != "orphaned"
    ]
    planned = point.traits_planned
    if planned is None:  # a manifest no sync has recorded traits for yet
        planned = [t for t in table.ids if any(e.trait == t for e in entries)]
    schema = point.attributes.get("schema_hash")

    checks: list[dict] = []
    exceptions: list[dict] = []

    def labels(trait_id: str) -> dict:
        """The trait's display code and the standards it answers, from the table."""
        trait = table.get(trait_id)
        return {
            "code": trait.code if trait else None,
            "standards": list(trait.standards) if trait else [],
        }

    def row(trait_id: str, entry: TestEntry | None) -> dict:
        if entry is None:
            return {
                "trait": trait_id,
                **labels(trait_id),
                "status": "not_run",
                "detail": "no test is registered for this check; run `itest sync`",
                "test": "",
            }
        outcome, detail = resolved.get(entry.canonical, ("missing", ""))
        raw = outcomes.get(entry.canonical) or {}
        check = raw.get("check")
        if check and outcome not in ("gated", "not_applicable"):
            status, text = check.get("status", "fail"), check.get("detail", "")
        else:
            status = _OUTCOME_STATUS.get(outcome, "not_run")
            text = _short_detail(outcome, detail, raw, environment)
        state = check_state(
            base_dir=base_dir,
            path=entry.path,
            test_name=entry.test_name,
            ownership_hash=entry.ownership_hash,
            point_schema=schema,
            retired=entry.retired,
        )
        result = {
            "trait": trait_id,
            **labels(trait_id),
            "status": status,
            "detail": text,
            "test": entry.canonical,
            "state": state,
        }
        if state == "stale":
            file = base_dir / entry.path
            frozen = _docstring_schema(file, entry.test_name)
            owned = stubgen.file_hash(file) == entry.ownership_hash
            message = (
                f"{entry.canonical} was generated against schema {frozen}; the "
                f"tool's schema is now {schema}. ITest owns the file: run "
                "`itest sync` to regenerate it."
                if owned
                else f"{entry.canonical} was edited by hand and generated "
                f"against schema {frozen}; the tool's schema is now "
                f"{schema}. Re-read it against the tool as it is."
            )
            exceptions.append(
                {
                    "kind": "stale",
                    "tool": point.target,
                    "trait": trait_id,
                    "message": message,
                }
            )
        elif status in ("critical", "fail", "changed"):
            exceptions.append(
                {
                    "kind": status,
                    "tool": point.target,
                    "trait": trait_id,
                    "message": text,
                }
            )
        return result

    for trait_id in planned:
        trait = table.get(trait_id)
        live = [e for e in entries if e.trait == trait_id and not e.retired]
        checks.append(
            row(trait_id, _pick_entry(live, trait.kind if trait else "generated"))
        )
    retired = {}
    for entry in entries:
        if entry.retired and entry.trait not in planned:
            retired.setdefault(entry.trait, entry)
    for trait_id, entry in retired.items():
        checks.append(row(trait_id, entry))
    return checks, exceptions


def _tool_verified(tool: dict, planned: list[str]) -> bool:
    """A coverage claim: every planned trait has a counted check that passed."""
    if not planned:
        return False
    by_trait = {c["trait"]: c for c in tool["checks"]}
    return all(
        (check := by_trait.get(trait_id)) is not None
        and check["status"] == "pass"
        and check.get("state") in _COUNTED
        for trait_id in planned
    )


def _tools_with(tools: list[dict], status: str) -> int:
    return sum(
        1 for tool in tools if any(c["status"] == status for c in tool["checks"])
    )


def build_tool_ledger(
    manifest: Manifest,
    base_dir: Path,
    resolved: dict[str, tuple[str, str]],
    outcomes: dict[str, dict],
    environment: str | None,
) -> dict | None:
    """The ``tools`` section of verify's JSON: one entry per declared server.

    Built only from what verify knows — the manifest's tool points and their
    ``traits_planned``, the test registered for each check, pytest's outcome
    and the ``CheckResult`` a check recorded — so nothing in it is illustrative:
    a check that did not run says ``not_run``, never ``pass``. ``None`` when the
    manifest records no declared tool.
    """
    tools = [p for p in manifest.points if p.type == "mcp_tool"]
    if not tools:
        return None
    from itest.core.declarations.traits import load_traits

    table = load_traits()
    run_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    servers = []
    for server in sorted({p.source for p in tools}):
        points_here = [p for p in tools if p.source == server]
        ids_here = {p.id for p in points_here}
        files_here = {t.path for t in manifest.tests if t.point_id in ids_here}
        prefix = stubgen.server_dir(server) + "/"

        entries: list[dict] = []
        exceptions: list[dict] = []
        verified = 0
        for point in points_here:
            checks, raised = _tool_checks(
                point, manifest, base_dir, table, resolved, outcomes, environment
            )
            counted = [c for c in checks if c.get("state") in _COUNTED]
            egress = point.attributes.get("egress")
            approval = point.attributes.get("approval")
            tool = {
                "name": point.target,
                "point_id": point.id,
                "mutation": str(point.attributes.get("mutation")),
                "mutation_source": str(point.attributes.get("mutation_source")),
                "egress": egress.get("to") if isinstance(egress, dict) else None,
                "approval": approval if approval and approval != "none" else None,
                "held_out": bool(counted)
                and all(c["status"] == "held_out" for c in counted),
                "schema_hash": point.attributes.get("schema_hash"),
                "description_hash": point.attributes.get("description_hash"),
                "checks": checks,
            }
            if _tool_verified(tool, point.traits_planned or []):
                verified += 1
            entries.append(tool)
            exceptions.extend(raised)

        orphans = [
            t
            for t in manifest.tests
            if t.status == "orphaned"
            and t.trait
            and (t.path in files_here or t.path.startswith(prefix))
        ]
        exceptions.extend(
            {
                "kind": "orphan",
                "tool": _tool_from_test_name(t),
                "trait": t.trait,
                "message": (
                    f"{t.canonical} covers a tool {server} no longer lists. Kept, "
                    "never deleted."
                ),
            }
            for t in orphans
        )
        order = {"stale": 0, "critical": 1, "fail": 2, "changed": 3, "orphan": 4}
        exceptions.sort(key=lambda e: order.get(e["kind"], 9))

        all_checks = [c for tool in entries for c in tool["checks"]]
        families = []
        for family_id, family_name in table.families.items():
            in_family = [
                c
                for c in all_checks
                if (trait := table.get(c["trait"])) is not None
                and trait.family == family_id
                and c.get("state") not in ("not_applicable", "orphan")
            ]
            families.append(
                {
                    "id": family_id,
                    "name": family_name,
                    "checked": sum(1 for c in in_family if c["status"] in _RAN),
                    "passed": sum(1 for c in in_family if c["status"] == "pass"),
                    "not_verifiable": sum(
                        1 for c in in_family if c["status"] == "not_verifiable"
                    ),
                }
            )

        servers.append(
            {
                "server": server,
                "environment": environment,
                "run_at": run_at,
                "declaration": points_here[0].hcl_address,
                "summary": {
                    "declared": len(points_here),
                    # Seen by the last sync: a held (unreachable) server's points
                    # keep the last_seen of the run that last saw them.
                    "live": sum(
                        1 for p in points_here if p.last_seen == manifest.generated_at
                    ),
                    "undeclared": 0,
                    "orphaned": len({t.point_id for t in orphans}),
                    "verified": verified,
                    "changed": _tools_with(entries, "changed"),
                    "held_out": sum(1 for tool in entries if tool["held_out"]),
                    "not_verifiable": sum(
                        1 for c in all_checks if c["status"] == "not_verifiable"
                    ),
                    "critical": _tools_with(entries, "critical"),
                },
                "families": families,
                "tools": entries,
                "exceptions": exceptions,
            }
        )
    # The standards lens: one entry per published id any check cites, plus
    # every OWASP Agentic entry nothing cites, as not covered. Derived here,
    # from the rows above, so it can never drift from them — and under the
    # same state filter as the families above: a retired or orphaned check is
    # not this run's evidence, so a standard only it cites is not covered.
    from itest.traits.standards import counted_checks, standards_rollup

    cited = counted_checks(
        c for server in servers for tool in server["tools"] for c in tool["checks"]
    )
    return {"servers": servers, "standards": standards_rollup(cited)}


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
    ran = sum(1 for t in report.tests if t.outcome not in ("gated", "not_applicable"))
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
