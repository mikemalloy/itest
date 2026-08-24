"""The ``itest verify`` engine.

Runs the pytest suite under ``itest_tests/``, maps each result back to the
integration point it covers via the manifest, and rolls results up to
point-level coverage. Output is available as a human table, JSON, or JUnit XML.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from itest.core import planner
from itest.core.manifest import load_manifest

JUNIT_NAME = "itest-results.xml"
_REPORT_NAME = "_verify_report.json"


class VerifyConfigError(Exception):
    """Raised for usage/config problems (maps to exit code 2)."""


class TestResult(BaseModel):
    canonical: str
    outcome: str  # passed | failed | skipped | missing
    point_id: str | None = None
    detail: str = ""


class PointResult(BaseModel):
    id: str
    source: str
    target: str
    attributes: dict = Field(default_factory=dict)
    status: str  # passing | failing | stub


class VerifyReport(BaseModel):
    total_points: int = 0
    passing: int = 0
    failing: int = 0
    stubs: int = 0
    orphaned_tests: int = 0
    points: list[PointResult] = Field(default_factory=list)
    tests: list[TestResult] = Field(default_factory=list)
    unregistered: list[str] = Field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.failing > 0 else 0


def _run_pytest(base_dir: Path, junit_path: Path | None) -> dict[str, dict]:
    """Run pytest on itest_tests/ and return {nodeid: {outcome, detail}}."""
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
    ]
    if junit_path is not None:
        args += ["--junitxml", str(junit_path)]

    env = dict(os.environ, ITEST_REPORT=str(report_file))
    subprocess.run(args, cwd=str(base_dir), env=env, capture_output=True, text=True)

    if not report_file.exists():
        return {}
    return json.loads(report_file.read_text(encoding="utf-8"))


def run_verify(base_dir: Path, output: str = "human") -> VerifyReport:
    """Execute the suite and build the coverage report."""
    manifest_file = planner.manifest_path(base_dir)
    if not manifest_file.exists():
        raise VerifyConfigError(
            "No manifest found. Run `itest plan && itest sync` first."
        )
    manifest = load_manifest(manifest_file)

    junit_path = base_dir / JUNIT_NAME if output == "junit" else None
    outcomes = _run_pytest(base_dir, junit_path)

    by_canonical = {t.canonical: t for t in manifest.tests}

    # Test-level results.
    test_results: list[TestResult] = []
    for test in manifest.tests:
        raw = outcomes.get(test.canonical)
        outcome = raw["outcome"] if raw else "missing"
        test_results.append(
            TestResult(
                canonical=test.canonical,
                outcome=outcome,
                point_id=test.point_id,
                detail=raw["detail"] if raw else "",
            )
        )
    unregistered = sorted(n for n in outcomes if n not in by_canonical)

    # Point-level rollup.
    point_results: list[PointResult] = []
    passing = failing = stubs = 0
    for point in manifest.points:
        live = [
            t
            for t in manifest.tests_for_point(point.id)
            if t.status != "orphaned" and not t.disabled
        ]
        live_outcomes = [
            outcomes.get(t.canonical, {}).get("outcome", "missing") for t in live
        ]
        if any(o == "failed" for o in live_outcomes):
            status = "failing"
            failing += 1
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

    return VerifyReport(
        total_points=len(manifest.points),
        passing=passing,
        failing=failing,
        stubs=stubs,
        orphaned_tests=orphaned_tests,
        points=point_results,
        tests=test_results,
        unregistered=unregistered,
    )


_STATUS_TAG = {"passing": "PASS", "failing": "FAIL", "stub": "STUB"}


def render_human(report: VerifyReport) -> str:
    out: list[str] = []
    out.append(
        f"{report.total_points} integration points: "
        f"{report.passing} passing, {report.failing} failing, "
        f"{report.stubs} stubs, {report.orphaned_tests} orphaned tests."
    )
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

    if report.unregistered:
        out.append("")
        out.append("Unregistered tests (not in manifest):")
        for n in report.unregistered:
            out.append(f"  {n}")

    return "\n".join(out)
