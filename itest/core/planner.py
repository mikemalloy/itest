"""The ``itest plan`` engine.

Reads a ``terraform show -json`` document, detects integration points, diffs
them against the existing manifest, and writes a proposed changeset plus a
Mermaid diagram. Planning is strictly read-only with respect to the manifest
and any test files.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from itest.core.detectors.base import detect_all
from itest.core.manifest import IntegrationPoint, TestEntry, load_manifest
from itest.core.mermaid import generate_mermaid

ITEST_DIR = ".itest"
MANIFEST_NAME = "manifest.yaml"
PLAN_NAME = "plan.json"
DIAGRAM_NAME = "diagram.mmd"


class PlanInputError(Exception):
    """Raised when plan JSON cannot be obtained (bad file or terraform error)."""


class Changeset(BaseModel):
    """The proposed change relative to the current manifest."""

    new_points: list[IntegrationPoint] = Field(default_factory=list)
    unchanged_points: list[IntegrationPoint] = Field(default_factory=list)
    orphan_candidates: list[TestEntry] = Field(default_factory=list)
    unanalyzed: dict[str, int] = Field(default_factory=dict)

    @property
    def detected_points(self) -> list[IntegrationPoint]:
        """All points detected this run (new + unchanged)."""
        return self.new_points + self.unchanged_points


def manifest_path(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / MANIFEST_NAME


def plan_path(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / PLAN_NAME


def diagram_path(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / DIAGRAM_NAME


def load_plan_json(tf_json: Path | None, base_dir: Path) -> dict:
    """Obtain the terraform plan JSON, from a file or by running terraform."""
    if tf_json is not None:
        path = Path(tf_json)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PlanInputError(f"--tf-json file not found: {path}") from None
        except json.JSONDecodeError as exc:
            raise PlanInputError(f"--tf-json file is not valid JSON: {exc}") from exc

    try:
        proc = subprocess.run(
            ["terraform", "show", "-json"],
            cwd=str(base_dir),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise PlanInputError(
            "terraform not found on PATH. Pass --tf-json PATH pointing at the "
            "output of `terraform show -json` instead."
        ) from None
    if proc.returncode != 0:
        raise PlanInputError(
            "`terraform show -json` failed:\n"
            f"{proc.stderr.strip()}\n"
            "Pass --tf-json PATH to supply the JSON directly."
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PlanInputError(
            f"`terraform show -json` did not return valid JSON: {exc}"
        ) from exc


def compute_changeset(
    points: list[IntegrationPoint],
    unanalyzed: dict[str, int],
    existing_point_ids: set[str],
    existing_tests: list[TestEntry],
) -> Changeset:
    """Diff detected points against what the manifest already knows."""
    detected_ids = {p.id for p in points}
    new_points = [p for p in points if p.id not in existing_point_ids]
    unchanged_points = [p for p in points if p.id in existing_point_ids]
    orphan_candidates = [
        t
        for t in existing_tests
        if t.point_id not in detected_ids and t.status != "orphaned"
    ]
    return Changeset(
        new_points=new_points,
        unchanged_points=unchanged_points,
        orphan_candidates=orphan_candidates,
        unanalyzed=unanalyzed,
    )


def run_plan(tf_json: Path | None, base_dir: Path) -> Changeset:
    """Full plan flow: detect, diff, and write plan.json + diagram.mmd.

    Does not touch the manifest or any test file.
    """
    plan_json = load_plan_json(tf_json, base_dir)
    points, unanalyzed = detect_all(plan_json)

    mpath = manifest_path(base_dir)
    if mpath.exists():
        manifest = load_manifest(mpath)
        existing_ids = {p.id for p in manifest.points}
        existing_tests = manifest.tests
    else:
        existing_ids = set()
        existing_tests = []

    changeset = compute_changeset(points, unanalyzed, existing_ids, existing_tests)

    itest_dir = base_dir / ITEST_DIR
    itest_dir.mkdir(parents=True, exist_ok=True)
    plan_path(base_dir).write_text(
        changeset.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    diagram_path(base_dir).write_text(
        generate_mermaid(changeset.detected_points), encoding="utf-8"
    )
    return changeset


def render_changeset(changeset: Changeset) -> str:
    """Human, Terraform-plan-style summary. Counts first, then detail."""
    n_new = len(changeset.new_points)
    n_unchanged = len(changeset.unchanged_points)
    n_orphan = len(changeset.orphan_candidates)

    out: list[str] = []
    out.append(
        f"ITest plan: {n_new} new, {n_unchanged} unchanged, "
        f"{n_orphan} orphaned test(s)."
    )
    out.append("")

    out.append(f"New integration points ({n_new}):")
    if changeset.new_points:
        for p in changeset.new_points:
            attrs = p.attributes
            tag = (
                f"{attrs.get('protocol')}:{attrs.get('ports')} {attrs.get('direction')}"
            )
            out.append(f"  + [{tag}] {p.source} -> {p.target}")
            out.append(f"      id={p.id}  hcl={p.hcl_address}")
    else:
        out.append("  (none)")
    out.append("")

    out.append(f"Orphan candidates ({n_orphan}):")
    if changeset.orphan_candidates:
        for t in changeset.orphan_candidates:
            out.append(f"  ~ {t.canonical}  (was point {t.point_id})")
    else:
        out.append("  (none)")
    out.append("")

    total_unanalyzed = sum(changeset.unanalyzed.values())
    out.append(f"Not analyzed ({total_unanalyzed} resource(s)):")
    if changeset.unanalyzed:
        width = max(len(t) for t in changeset.unanalyzed)
        for rtype in sorted(changeset.unanalyzed):
            out.append(f"  {rtype.ljust(width)}  {changeset.unanalyzed[rtype]}")
    else:
        out.append("  (none)")

    return "\n".join(out)
