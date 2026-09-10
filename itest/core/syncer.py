"""The ``itest sync`` engine.

Turns a plan changeset into concrete state: it generates test stubs for new
integration points, flags orphaned tests, and rewrites the manifest. The one
inviolable rule (DESIGN.md): a test file whose content hash differs from the
recorded ownership hash is human-modified — sync appends to it but never
rewrites or deletes a function in it.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel

from itest.core import planner, stubgen
from itest.core.manifest import (
    IntegrationPoint,
    Manifest,
    TestEntry,
    Tier,
    load_manifest,
    save_manifest,
)
from itest.core.planner import Changeset

if TYPE_CHECKING:  # types only: sync imports no trait machinery up front
    from itest.core.declarations.traits import Trait

#: The declared point type. Spelled out rather than imported so that a
#: terraform-only sync never loads the declarations package — and through it a
#: probe transport. A test pins this against the real constant, so the two
#: cannot drift apart.
_TOOL_POINT_TYPE = "mcp_tool"

#: What a stub generated for a detected point is registered as, unchanged from
#: before declarations existed. A declared point's tier comes from the trait.
_DEFAULT_TIER: Tier = "readonly"


class SyncResult(BaseModel):
    """Outcome of an applied sync, for the git-style summary line."""

    added_stubs: int = 0
    flagged_orphans: int = 0
    resurrected_tests: int = 0
    reclassified_tests: int = 0
    #: Points whose attributes drifted and were re-recorded. No stub is added and
    #: nothing is orphaned, so without this clause the line would read as though
    #: sync had done nothing while the manifest changed underneath it.
    recorded_changes: int = 0
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
        # Append-only, like the clauses above it: absent unless something drifted.
        recorded = (
            f"recorded {self.recorded_changes} changed point(s), "
            if self.recorded_changes
            else ""
        )
        return (
            f"Applied: added {self.added_stubs} stub(s), "
            f"flagged {self.flagged_orphans} orphan(s), "
            f"{resurrected}"
            f"{reclassified}"
            f"{recorded}"
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
    """True when there is nothing to apply.

    A *changed* point counts as something to apply even though it adds no stub
    and orphans nothing: the drifted attribute has to be recorded, or the next
    run would report the same drift forever.
    """
    return not (
        changeset.new_points
        or changeset.resurrected_points
        or changeset.changed_points
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
        recorded_changes=len(changeset.changed_points),
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

    Resolved by AST (the same parse register.py uses), not string-matching, so
    the definition classified is the one Python would bind: the *last*
    module-level ``def``/``async def`` of that name shadows earlier ones, and a
    same-named class method is only consulted when there is no module-level
    definition at all. String-matching found the first textual ``def name(`` and
    read to the next top-level ``def``, which misreads a nested method and picks
    the wrong definition when a name is shadowed.
    """
    if not path.exists():
        return "stub"
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # An unparseable file has no implemented body we can trust.
        return "stub"

    def named(nodes) -> list[ast.AST]:
        return [
            n
            for n in nodes
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            and n.name == test_name
        ]

    # Module-level first (a top-level def is what sync generates and what a
    # canonical `path::name` addresses); fall back to any nested definition.
    matches = named(tree.body) or named(ast.walk(tree))
    if not matches:
        return "stub"
    segment = ast.get_source_segment(text, matches[-1]) or ""
    return "stub" if stubgen.STUB_SKIP_LINE in segment else "implemented"


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


class _PendingStub(NamedTuple):
    """One stub to generate: a point, optionally a trait, and where it goes.

    A detected point yields exactly one stub (``trait`` is ``None``). A declared
    tool yields **one per applicable trait**, which is why this exists: routing
    and naming can no longer be a function of the point alone.
    """

    point: IntegrationPoint
    trait: Trait | None
    file_rel: str
    tier: Tier

    def function_name(self) -> str:
        if self.trait is None:
            return stubgen.function_name_for(self.point)
        return stubgen.tool_function_name(self.point, self.trait.id)

    def entry_id(self) -> str:
        if self.trait is None:
            return f"t-{self.point.id}"
        # One point now carries several tests, so the trait is part of the id.
        return f"t-{self.point.id}-{self.trait.id.lower()}"

    def render(self, func_name: str) -> str:
        if self.trait is None:
            return stubgen.render_stub(self.point, func_name)
        return stubgen.render_tool_stub(self.point, func_name, self.trait)


def _traits_for(point: IntegrationPoint, table) -> list[Trait]:
    """Which checks one declared tool gets.

    The table decides, unless the declaration hand-picked a list — and a tool
    whose declaration withholds the active tier loses its active traits whichever
    way they were chosen. Withholding removes the stub; it never leaves one
    behind that must not run.
    """
    from itest.core.declarations.schema import NO_TRAITS
    from itest.core.declarations.tools import trait_context
    from itest.core.declarations.traits import TraitTableError, applicable

    declared = point.attributes.get("traits")
    if declared:
        if NO_TRAITS in declared:
            chosen = []
        else:
            chosen = []
            for trait_id in declared:
                trait = table.get(trait_id)
                if trait is None:
                    raise TraitTableError(
                        f"{point.source}/{point.target} is declared with trait "
                        f"{trait_id!r}, which the trait table does not define. "
                        f"Known traits: {', '.join(table.ids)}."
                    )
                chosen.append(trait)
    else:
        chosen = applicable(table, trait_context(point))

    if point.attributes.get("active") is False:
        return [trait for trait in chosen if trait.tier != "active"]
    return chosen


def _plan_stubs(changeset: Changeset) -> list[_PendingStub]:
    """Expand the changeset's new points into the stubs sync will write.

    The trait table is loaded lazily and only when a declared point is present,
    so a project with no declarations does not read it at all.
    """
    table = None
    pending: list[_PendingStub] = []
    for point in changeset.new_points:
        if point.type != _TOOL_POINT_TYPE:
            pending.append(
                _PendingStub(point, None, stubgen.stub_file_for(point), _DEFAULT_TIER)
            )
            continue
        if table is None:
            from itest.core.declarations.traits import load_traits

            table = load_traits()
        for trait in _traits_for(point, table):
            pending.append(
                _PendingStub(
                    point,
                    trait,
                    stubgen.tool_stub_file_for(point, trait.tier),
                    trait.tier,
                )
            )
    return pending


def _generate_stubs(
    manifest: Manifest, changeset: Changeset, base_dir: Path
) -> tuple[int, int]:
    """Append stubs, routing each one to its file.

    Returns ``(added, human_modified_file_count)``. Every guarantee is *per
    file*: its own recorded hashes, its own human-modified verdict, and its own
    set of used function names. A file is only ever appended to, and an entry
    already in the manifest keeps the path it was recorded with — routing
    applies to new stubs, never to tests that already exist.
    """
    point_targets = {p.id: p.target for p in changeset.new_points}

    routed: dict[str, list[_PendingStub]] = {}
    for stub in _plan_stubs(changeset):
        routed.setdefault(stub.file_rel, []).append(stub)

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

        new_stubs = routed.get(file_rel)
        if not new_stubs:
            continue

        used_names = {t.test_name for t in manifest.tests if t.path == file_rel}
        blocks: list[str] = []
        pending: list[tuple[_PendingStub, str]] = []
        for stub in new_stubs:
            name = stub.function_name()
            if name in used_names:
                name = f"{name}_{stub.point.id[:6]}"
            used_names.add(name)
            blocks.append(stub.render(name))
            pending.append((stub, name))

        # Append-only: existing functions (including human edits) are kept.
        stubgen.append_stubs(file_abs, blocks)
        final_hash = stubgen.file_hash(file_abs)

        for stub, name in pending:
            manifest.tests.append(
                TestEntry(
                    id=stub.entry_id(),
                    point_id=stub.point.id,
                    path=file_rel,
                    test_name=name,
                    ownership_hash=final_hash,
                    status="stub",
                    tier=stub.tier,
                    resource_group=point_targets.get(stub.point.id),
                )
            )

        # Every entry in *this* file records this file's new content hash.
        for test in manifest.tests:
            if test.path == file_rel:
                test.ownership_hash = final_hash

        added += len(pending)

    return added, human_modified_files
