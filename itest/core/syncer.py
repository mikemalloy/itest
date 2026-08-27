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
from typing import Literal

from pydantic import BaseModel

from itest.core import planner, stubgen
from itest.core.manifest import (
    IntegrationPoint,
    Manifest,
    TestEntry,
    load_manifest,
    save_manifest,
)
from itest.core.planner import Changeset


class SyncResult(BaseModel):
    """Outcome of an applied sync, for the git-style summary line."""

    added_stubs: int = 0
    flagged_orphans: int = 0
    resurrected_tests: int = 0
    reclassified_tests: int = 0
    human_modified_files: int = 0

    def summary(self) -> str:
        resurrected = (
            f"resurrected {self.resurrected_tests} test(s), "
            if self.resurrected_tests
            else ""
        )
        reclassified = (
            f"reclassified {self.reclassified_tests} test(s), "
            if self.reclassified_tests
            else ""
        )
        return (
            f"Applied: added {self.added_stubs} stub(s), "
            f"flagged {self.flagged_orphans} orphan(s), "
            f"{resurrected}"
            f"{reclassified}"
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
    """True when there is nothing to apply: no new points, resurrections, or
    orphans."""
    return not (
        changeset.new_points
        or changeset.resurrected_points
        or changeset.orphan_candidates
    )


def apply(changeset: Changeset, base_dir: Path) -> SyncResult:
    """Apply the changeset: generate stubs, flag orphans, rewrite the manifest."""
    now = datetime.now(UTC)
    manifest_file = planner.manifest_path(base_dir)
    if manifest_file.exists():
        manifest = load_manifest(manifest_file)
    else:
        manifest = Manifest(generated_at=now, points=[], tests=[])

    _refresh_point_registry(manifest, changeset, now)
    resurrected = _resurrect_tests(manifest, changeset, base_dir)
    flagged = _flag_orphans(manifest, changeset)
    added, human_modified_files = _generate_stubs(manifest, changeset, base_dir)
    # Last, so it reads the stubs this run just wrote as well as the ones a
    # human implemented since the previous run.
    reclassified = _reclassify_statuses(manifest, base_dir)

    manifest.generated_at = now
    save_manifest(manifest, manifest_file)

    return SyncResult(
        added_stubs=added,
        flagged_orphans=flagged,
        resurrected_tests=resurrected,
        reclassified_tests=reclassified,
        human_modified_files=human_modified_files,
    )


def reconcile(base_dir: Path) -> int:
    """Reclassify statuses from bodies when the changeset itself is a no-op.

    ``apply`` is skipped entirely when a plan proposes nothing (DESIGN.md's
    plan/apply model), but a human implementing a stub changes no plan -- so
    without this the manifest would never learn that the test is implemented.
    Returns the number of entries whose status changed; writes only when one
    did, so a genuine no-op leaves the file untouched.
    """
    manifest_file = planner.manifest_path(base_dir)
    if not manifest_file.exists():
        return 0

    manifest = load_manifest(manifest_file)
    changed = _reclassify_statuses(manifest, base_dir)
    if changed:
        save_manifest(manifest, manifest_file)
    return changed


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


def _status_from_body(path: Path, test_name: str) -> Literal["stub", "implemented"]:
    """Classify a test by whether its body still holds the generated skip line.

    Read back from disk rather than trusted from the manifest: the whole point
    of a resurrection is that a human may have implemented the test in the
    meantime.
    """
    if not path.exists():
        return "stub"
    text = path.read_text(encoding="utf-8")
    start = text.find(f"def {test_name}(")
    if start == -1:
        return "stub"
    # The function body runs to the next top-level def, or to end of file.
    rest = text[start:]
    next_def = rest.find("\ndef ", 1)
    body = rest if next_def == -1 else rest[:next_def]
    return "stub" if stubgen.STUB_SKIP_LINE in body else "implemented"


def _reclassify_statuses(manifest: Manifest, base_dir: Path) -> int:
    """Re-derive every live test's status from the body on disk.

    The body is the truth: a test holding the generated skip line is a stub,
    and one that does not is implemented. Orphaned entries are left alone --
    orphaning is a statement about the point, not about the body, and only a
    resurrection may lift it.

    Returns the number of entries whose recorded status changed.
    """
    changed = 0
    for test in manifest.tests:
        if test.status == "orphaned":
            continue
        status = _status_from_body(base_dir / test.path, test.test_name)
        if status != test.status:
            test.status = status
            changed += 1
    return changed


def _resurrect_tests(manifest: Manifest, changeset: Changeset, base_dir: Path) -> int:
    """Un-orphan the tests of points that have come back.

    A returning point already has a test on disk, so it is re-linked to that
    test instead of being given a fresh stub.
    """
    returning_ids = {p.id for p in changeset.resurrected_points}
    if not returning_ids:
        return 0

    resurrected = 0
    for test in manifest.tests:
        if test.point_id in returning_ids and test.status == "orphaned":
            test.status = _status_from_body(base_dir / test.path, test.test_name)
            resurrected += 1
    return resurrected


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
) -> tuple[int, int]:
    """Append stubs, routing each point to the file for its type.

    Returns ``(added, human_modified_file_count)``. Every guarantee is *per
    file*: its own recorded hashes, its own human-modified verdict, and its own
    set of used function names. A file is only ever appended to, and an entry
    already in the manifest keeps the path it was recorded with — routing
    applies to new stubs, never to tests that already exist.
    """
    point_targets = {p.id: p.target for p in changeset.new_points}

    routed: dict[str, list[IntegrationPoint]] = {}
    for point in changeset.new_points:
        routed.setdefault(stubgen.stub_file_for(point), []).append(point)

    # Every file the manifest already knows is checked for human edits, even
    # when this sync adds nothing to it — as the single-file version did.
    known_files = {t.path for t in manifest.tests} | set(routed)

    added = 0
    human_modified_files = 0
    for file_rel in sorted(known_files):
        file_abs = stubgen.stub_file_path(base_dir, file_rel)

        recorded_hashes = {
            t.ownership_hash for t in manifest.tests if t.path == file_rel
        }
        if (
            file_abs.exists()
            and recorded_hashes
            and stubgen.file_hash(file_abs) not in recorded_hashes
        ):
            human_modified_files += 1

        new_points = routed.get(file_rel)
        if not new_points:
            continue

        used_names = {t.test_name for t in manifest.tests if t.path == file_rel}
        blocks: list[str] = []
        pending: list[tuple[str, str]] = []  # (point_id, func_name)
        for point in new_points:
            name = stubgen.function_name_for(point)
            if name in used_names:
                name = f"{name}_{point.id[:6]}"
            used_names.add(name)
            blocks.append(stubgen.render_stub(point, name))
            pending.append((point.id, name))

        # Append-only: existing functions (including human edits) are kept.
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
                    resource_group=point_targets.get(point_id),
                )
            )

        # Every entry in *this* file records this file's new content hash.
        for test in manifest.tests:
            if test.path == file_rel:
                test.ownership_hash = final_hash

        added += len(pending)

    return added, human_modified_files
