"""The ``itest sync`` engine.

Turns a plan changeset into concrete state: it generates test stubs for new
integration points, flags orphaned tests, and rewrites the manifest. The one
inviolable rule (DESIGN.md): a test file whose content hash differs from the
recorded ownership hash is human-modified — sync appends to it but never
rewrites or deletes a function in it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from itest.core import planner, stubgen
from itest.core.manifest import Manifest, TestEntry, load_manifest, save_manifest
from itest.core.planner import Changeset


class SyncResult(BaseModel):
    """Outcome of an applied sync, for the git-style summary line."""

    added_stubs: int = 0
    flagged_orphans: int = 0
    human_modified_files: int = 0

    def summary(self) -> str:
        return (
            f"Applied: added {self.added_stubs} stub(s), "
            f"flagged {self.flagged_orphans} orphan(s), "
            f"{self.human_modified_files} human-modified file(s) preserved."
        )


def prepare(tf_json: Path | None, base_dir: Path) -> tuple[Changeset, str | None]:
    """Obtain a current changeset, re-planning when needed.

    Re-plans (and refreshes plan.json/diagram.mmd) when plan.json is missing,
    older than the manifest, or when an explicit --tf-json was supplied.
    Returns the changeset and an optional note explaining a re-plan.
    """
    plan_file = planner.plan_path(base_dir)
    manifest_file = planner.manifest_path(base_dir)

    need_replan = tf_json is not None or not plan_file.exists()
    if not need_replan and manifest_file.exists():
        if plan_file.stat().st_mtime < manifest_file.stat().st_mtime:
            need_replan = True

    if need_replan:
        changeset = planner.run_plan(tf_json, base_dir)
        return changeset, (
            "Ran plan first (plan.json missing, stale, or --tf-json given)."
        )

    changeset = Changeset.model_validate_json(plan_file.read_text(encoding="utf-8"))
    return changeset, None


def is_noop(changeset: Changeset) -> bool:
    """True when there is nothing to apply: no new points and no orphans."""
    return not changeset.new_points and not changeset.orphan_candidates


def apply(changeset: Changeset, base_dir: Path) -> SyncResult:
    """Apply the changeset: generate stubs, flag orphans, rewrite the manifest."""
    now = datetime.now(UTC)
    manifest_file = planner.manifest_path(base_dir)
    if manifest_file.exists():
        manifest = load_manifest(manifest_file)
    else:
        manifest = Manifest(generated_at=now, points=[], tests=[])

    _refresh_point_registry(manifest, changeset, now)
    flagged = _flag_orphans(manifest, changeset)
    added, human_modified = _generate_stubs(manifest, changeset, base_dir)

    manifest.generated_at = now
    save_manifest(manifest, manifest_file)

    return SyncResult(
        added_stubs=added,
        flagged_orphans=flagged,
        human_modified_files=1 if human_modified else 0,
    )


def _refresh_point_registry(
    manifest: Manifest, changeset: Changeset, now: datetime
) -> None:
    """Replace the point registry with what was detected, preserving first_seen."""
    existing = {p.id: p for p in manifest.points}
    registry = []
    for point in changeset.detected_points:
        if point.id in existing:
            first_seen = existing[point.id].first_seen
        else:
            first_seen = now
        registry.append(
            point.model_copy(update={"first_seen": first_seen, "last_seen": now})
        )
    manifest.points = registry


def _flag_orphans(manifest: Manifest, changeset: Changeset) -> int:
    orphan_ids = {t.id for t in changeset.orphan_candidates}
    flagged = 0
    for test in manifest.tests:
        if test.id in orphan_ids and test.status != "orphaned":
            test.status = "orphaned"
            flagged += 1
    return flagged


def _generate_stubs(
    manifest: Manifest, changeset: Changeset, base_dir: Path
) -> tuple[int, bool]:
    """Append stubs for new points. Returns (added, file_was_human_modified)."""
    file_rel = stubgen.STUB_FILE_REL
    file_abs = stubgen.stub_file_path(base_dir)

    recorded_hashes = {t.ownership_hash for t in manifest.tests if t.path == file_rel}
    human_modified = False
    if file_abs.exists() and recorded_hashes:
        if stubgen.file_hash(file_abs) not in recorded_hashes:
            human_modified = True

    used_names = {t.test_name for t in manifest.tests if t.path == file_rel}
    blocks: list[str] = []
    pending: list[tuple[str, str]] = []  # (point_id, func_name)
    for point in changeset.new_points:
        name = stubgen.function_name_for(point)
        if name in used_names:
            name = f"{name}_{point.id[:6]}"
        used_names.add(name)
        blocks.append(stubgen.render_stub(point, name))
        pending.append((point.id, name))

    if not blocks:
        return 0, human_modified

    # Append-only: existing functions (including any human edits) are preserved.
    stubgen.append_stubs(file_abs, blocks)
    final_hash = stubgen.file_hash(file_abs)

    for point_id, name in pending:
        manifest.tests.append(
            TestEntry(
                id=f"t-{point_id}",
                point_id=point_id,
                path=file_rel,
                test_name=name,
                ownership_hash=final_hash,
                status="stub",
            )
        )

    # Update ownership hashes for every entry in this file to the new content.
    for test in manifest.tests:
        if test.path == file_rel:
            test.ownership_hash = final_hash

    return len(pending), human_modified
