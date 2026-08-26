"""The ``itest verify`` engine.

Runs the pytest suite under ``itest_tests/``, maps each result back to the
integration point it covers via the manifest, and rolls results up to
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

from itest.core import planner
from itest.core.manifest import load_manifest, save_manifest

JUNIT_NAME = "itest-results.xml"
_REPORT_NAME = "_verify_report.json"


class VerifyConfigError(Exception):
    """Raised for usage/config problems (maps to exit code 2)."""


class TestResult(BaseModel):
    canonical: str
    outcome: str  # passed | failed | skipped | error | missing
    point_id: str | None = None
    detail: str = ""


class PointResult(BaseModel):
    id: str
    source: str
    target: str
    attributes: dict = Field(default_factory=dict)
    status: str  # passing | failing | error | stub


class VerifyReport(BaseModel):
    total_points: int = 0
    passing: int = 0
    failing: int = 0
    errored: int = 0
    stubs: int = 0
    orphaned_tests: int = 0
    elapsed_seconds: float = 0.0
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


def _run_pytest(
    base_dir: Path, junit_path: Path | None
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Run pytest on itest_tests/ and return (test outcomes, collection errors)."""
    report_file = base_dir / planner.ITEST_DIR / _REPORT_NAME
    report_file.parent.mkdir(parents=True, exist_ok=True)
    if report_file.exists():
        report_file.unlink()

    args = [
        sys.executable,
        "-m",
        "pytest",
        "itest_tests",
        "-q",
        "-p",
        "itest.core._pytest_report",
        "-p",
        "no:cacheprovider",
        # Without this, the first un-importable module aborts collection and
        # nothing else runs — one broken file would blind every other point.
        "--continue-on-collection-errors",
    ]
    if junit_path is not None:
        args += ["--junitxml", str(junit_path)]

    env = dict(os.environ, ITEST_REPORT=str(report_file))
    subprocess.run(args, cwd=str(base_dir), env=env, capture_output=True, text=True)

    if not report_file.exists():
        return {}, {}
    document = json.loads(report_file.read_text(encoding="utf-8"))
    return document.get("tests", {}), document.get("collection_errors", {})


def run_verify(base_dir: Path, output: str = "human") -> VerifyReport:
    """Execute the suite and build the coverage report."""
    manifest_file = planner.manifest_path(base_dir)
    if not manifest_file.exists():
        raise VerifyConfigError(
            "No manifest found. Run `itest plan && itest sync` first."
        )
    manifest = load_manifest(manifest_file)
    _require_pytest()

    junit_path = base_dir / JUNIT_NAME if output == "junit" else None
    started = time.monotonic()
    outcomes, collection_errors = _run_pytest(base_dir, junit_path)
    elapsed = time.monotonic() - started

    by_canonical = {t.canonical: t for t in manifest.tests}

    # Resolve every registered test once. A test in a module that failed to
    # collect has no per-test outcome, so it inherits its module's error rather
    # than looking merely absent.
    resolved: dict[str, tuple[str, str]] = {}
    durations_recorded = False
    for test in manifest.tests:
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

    # Point-level rollup.
    point_results: list[PointResult] = []
    passing = failing = errored = stubs = 0
    for point in manifest.points:
        live = [
            t
            for t in manifest.tests_for_point(point.id)
            if t.status != "orphaned" and not t.disabled
        ]
        live_outcomes = [resolved[t.canonical][0] for t in live]
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
        point_results.append(
            PointResult(
                id=point.id,
                source=point.source,
                target=point.target,
                attributes=point.attributes,
                status=status,
            )
        )

    orphaned_tests = sum(1 for t in manifest.tests if t.status == "orphaned")

    # Persist per-test durations (schema v2). This is verify's only write to
    # the manifest; test registration and status stay sync's job.
    if durations_recorded:
        save_manifest(manifest, manifest_file)

    return VerifyReport(
        total_points=len(manifest.points),
        passing=passing,
        failing=failing,
        errored=errored,
        stubs=stubs,
        orphaned_tests=orphaned_tests,
        elapsed_seconds=round(elapsed, 2),
        points=point_results,
        tests=test_results,
        unregistered=unregistered,
    )


_STATUS_TAG = {
    "passing": "PASS",
    "failing": "FAIL",
    "error": "ERROR",
    "stub": "STUB",
}


def render_human(report: VerifyReport) -> str:
    out: list[str] = []
    out.append(
        f"{report.total_points} integration points: "
        f"{report.passing} passing, {report.failing} failing, "
        f"{report.errored} errored, "
        f"{report.stubs} stubs, {report.orphaned_tests} orphaned tests."
    )
    out.append(f"Ran {len(report.tests)} tests in {report.elapsed_seconds:.2f}s")
    out.append("")
    out.append("Points:")
    for p in report.points:
        tag = _STATUS_TAG.get(p.status, "????")
        proto = p.attributes.get("protocol", "")
        ports = p.attributes.get("ports", "")
        out.append(f"  [{tag}] {p.source} -> {p.target} ({proto}:{ports})")

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

    return "\n".join(out)
