"""The ``itest sync`` engine.

Turns a plan changeset into concrete state: it generates test stubs for new
integration points, flags orphaned tests, and rewrites the manifest. The one
inviolable rule (DESIGN.md): a test file whose content hash differs from the
recorded ownership hash is human-modified — sync appends to it but never
rewrites or deletes a function in it.

For declared tools it also applies the trait plan: it records each tool's
``traits_planned`` and the table hash, appends a stub for every applicable
*generated* trait that has none (an *engine* trait needs no per-tool code), and
**retires** the test of a trait that no longer applies — kept on disk and in the
manifest, never run — restoring the same entry if the trait applies again.
Nothing generated is ever deleted.

A binding ITest still owns (its file matches the recorded ownership hash) whose
frozen ``schema:`` is not the tool's current schema is **regenerated**: owning a
file is what lets ITest bring it up to date, and a check left frozen against a
schema the tool no longer has is ``stale`` in verify. A hand-edited file is
never touched; verify reports it.
"""

from __future__ import annotations

import ast
import re
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
from itest.traits.ids import LEGACY_BY_TRAIT

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
    #: Per-trait tests whose trait stopped applying, and ones that came back.
    retired_checks: int = 0
    restored_checks: int = 0
    #: ITest-owned bindings re-frozen against their tool's current schema.
    regenerated_checks: int = 0
    #: Engine cases newly registered: (tool, engine trait) pairs the engine
    #: module now runs. No code is written for them.
    registered_engine_checks: int = 0
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
        regenerated = (
            f"regenerated {self.regenerated_checks} check(s), "
            if self.regenerated_checks
            else ""
        )
        retired = (
            f"retired {self.retired_checks} check(s), " if self.retired_checks else ""
        )
        restored = (
            f"restored {self.restored_checks} check(s), "
            if self.restored_checks
            else ""
        )
        engine = (
            f"registered {self.registered_engine_checks} engine check(s), "
            if self.registered_engine_checks
            else ""
        )
        return (
            f"Applied: added {self.added_stubs} stub(s), "
            f"flagged {self.flagged_orphans} orphan(s), "
            f"{resurrected}"
            f"{reclassified}"
            f"{recorded}"
            f"{regenerated}"
            f"{engine}"
            f"{retired}"
            f"{restored}"
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
    if changeset.trait_table_hash is not None and _table_moved(changeset):
        changeset = planner.run_plan(tf_json, base_dir)
        return changeset, "Ran plan first (the trait table changed since plan.json)."
    return changeset, None


def _table_moved(changeset: Changeset) -> bool:
    """True when the table on disk is not the one plan.json was computed from."""
    from itest.core.declarations.traits import trait_table_hash

    return trait_table_hash() != changeset.trait_table_hash


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
        or changeset.trait_changes
        # A table hash not yet recorded (a first sync after an upgrade, or a
        # table edit that happened to move no check) still has to be written.
        or changeset.trait_table_hash != changeset.previous_trait_table_hash
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
    if changeset.trait_table_hash is not None:
        manifest.trait_table_hash = changeset.trait_table_hash
    resurrected = _resurrect_tests(manifest, changeset, base_dir)
    flagged = _flag_orphans(manifest, changeset)
    _record_entry_traits(manifest)
    retired, restored = _apply_trait_lifecycle(manifest, changeset)
    # Before any append: a regenerated file records its new hash first, so the
    # human-modified check below still reads it as ITest's.
    regenerated = _regenerate_owned_bindings(manifest, base_dir)
    engine = _write_tool_support(manifest, changeset, base_dir)
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
        retired_checks=retired,
        restored_checks=restored,
        regenerated_checks=regenerated,
        registered_engine_checks=engine,
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


def persist_migration(base_dir: Path) -> bool:
    """Write back a manifest that was read with old AN-style trait ids.

    Loading maps the old ids to the new ones in memory; this is the "rewrite on
    the next sync" half, for the sync whose plan is otherwise a no-op. Returns
    whether the file was rewritten.
    """
    manifest_file = planner.manifest_path(base_dir)
    if not manifest_file.exists():
        return False
    manifest = load_manifest(manifest_file)
    if not manifest.trait_ids_migrated:
        return False
    save_manifest(manifest, manifest_file)
    return True


def regenerate(base_dir: Path) -> int:
    """Regenerate owned bindings when the changeset itself is a no-op.

    The manifest can already record a tool's current schema while a binding is
    still frozen against the old one (a sync from before regeneration existed).
    No plan moves, so ``apply`` never runs; this is what brings the file up to
    date. Returns the number of bindings regenerated; writes only when one was.
    """
    manifest_file = planner.manifest_path(base_dir)
    if not manifest_file.exists():
        return 0
    manifest = load_manifest(manifest_file)
    regenerated = _regenerate_owned_bindings(manifest, base_dir)
    if regenerated:
        save_manifest(manifest, manifest_file)
    return regenerated


#: A generated binding's frozen docstring line (``stubgen.render_generated_stub``).
_BINDING_DOCSTRING = re.compile(
    r'^(?P<head>[ \t]*"""itest point: (?P<point>\S+)[ \t]+trait: \S+[ \t]+schema: )'
    r'(?P<schema>\S+?)(?P<tail>"""[ \t]*)$',
    re.MULTILINE,
)


def _regenerate_owned_bindings(manifest: Manifest, base_dir: Path) -> int:
    """Re-freeze every ITest-owned binding against its tool's current schema.

    A binding's body does not depend on the schema; its docstring records the
    one it was generated against, and that is what verify compares. So
    regenerating one is rewriting that line — only in a file whose content
    still matches its recorded ownership hash. A hand-edited file is frozen:
    left byte for byte, and reported by verify as ``stale``. A regenerated file
    records its new hash on every entry it holds, so it stays ITest's. Returns
    the number of bindings regenerated.
    """
    schemas = {
        p.id: p.attributes.get("schema_hash")
        for p in manifest.points
        if p.type == _TOOL_POINT_TYPE
    }
    files = sorted(
        {
            t.path
            for t in manifest.tests
            if t.point_id in schemas
            and t.trait is not None
            and t.status != "orphaned"
            and not _is_engine_case(t)
        }
    )
    regenerated = 0
    for file_rel in files:
        file_abs = stubgen.stub_file_path(base_dir, file_rel)
        recorded = {t.ownership_hash for t in manifest.tests if t.path == file_rel}
        if not file_abs.exists() or stubgen.file_hash(file_abs) not in recorded:
            continue  # hand-edited (or gone): frozen, and verify says so
        moved = 0

        def refreeze(match: re.Match) -> str:
            nonlocal moved
            current = schemas.get(match["point"])
            if not current or current == match["schema"]:
                return match.group(0)
            moved += 1
            return f"{match['head']}{current}{match['tail']}"

        text = _BINDING_DOCSTRING.sub(refreeze, file_abs.read_text(encoding="utf-8"))
        if not moved:
            continue
        file_abs.write_text(text, encoding="utf-8")
        owned = stubgen.file_hash(file_abs)
        for test in manifest.tests:
            if test.path == file_rel:
                test.ownership_hash = owned
        regenerated += moved
    return regenerated


def _refresh_point_registry(
    manifest: Manifest, changeset: Changeset, now: datetime
) -> None:
    """Replace the point registry with what was detected, preserving first_seen.

    Held points (an unreachable server's) are kept exactly as the manifest has
    them, ``last_seen`` included: nothing saw them this run.
    """
    existing = {p.id: p for p in manifest.points}
    registry = []
    for point in changeset.detected_points:
        if point.id in existing:
            first_seen = existing[point.id].first_seen
        else:
            first_seen = now
        update = {"first_seen": first_seen, "last_seen": now}
        if point.id in changeset.traits_planned:
            update["traits_planned"] = list(changeset.traits_planned[point.id])
        registry.append(point.model_copy(update=update))
    registry.extend(existing.get(p.id, p) for p in changeset.held_points)
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

    # A parametrized case (`test_engine[tool-<trait>]`) is its function's body.
    test_name = test_name.split("[", 1)[0]
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
        return stubgen.render_generated_stub(self.point, func_name, self.trait)

    @property
    def header(self) -> str:
        return stubgen.FILE_HEADER if self.trait is None else stubgen.GENERATED_HEADER


def _record_entry_traits(manifest: Manifest) -> None:
    """Fill ``trait`` on a declared tool's per-trait entries that predate it.

    A P30 manifest encoded the trait only in the entry id sync gave it.
    """
    tool_ids = {p.id for p in manifest.points if p.type == _TOOL_POINT_TYPE}
    for test in manifest.tests:
        if test.trait is None and test.point_id in tool_ids:
            test.trait = planner.trait_from_entry(test)


def _trait_kinds() -> dict[str, str]:
    """trait id -> ``engine`` | ``generated``, from the table. Loaded lazily."""
    from itest.core.declarations.traits import load_traits

    return {trait.id: trait.kind for trait in load_traits().traits}


def _superseded(test: TestEntry, kinds: dict[str, str]) -> bool:
    """A per-tool stub sync wrote for a trait the engine module now runs.

    Before the engine module existed, sync wrote one stub per (tool, trait) for
    every trait, engine ones included (entry id ``t-<point>-<trait>``, with the
    trait's old AN-style id). Such a stub would run as a skip beside the engine
    case that actually checks the trait, so it is retired in place. Only sync's
    own per-tool stubs match: an engine case (``e-`` id) and a test a human
    registered keep running.
    """
    if test.trait is None:
        return False
    legacy = LEGACY_BY_TRAIT.get(test.trait)
    sync_ids = {f"t-{test.point_id}-{test.trait.lower()}"}
    if legacy:
        sync_ids.add(f"t-{test.point_id}-{legacy.lower()}")
    return (
        kinds.get(test.trait) == "engine"
        and not _is_engine_case(test)
        and test.id in sync_ids
    )


def retire_superseded(base_dir: Path) -> int:
    """Retire superseded per-tool engine stubs when the changeset is a no-op.

    A checkout synced before this rule records ``traits_planned`` already, so
    its plan moves nothing and ``apply`` never runs — while its P30 engine stubs
    are still live. Returns how many were retired; writes only when one was.
    """
    manifest_file = planner.manifest_path(base_dir)
    if not manifest_file.exists():
        return 0
    manifest = load_manifest(manifest_file)
    tool_ids = {p.id for p in manifest.points if p.type == _TOOL_POINT_TYPE}
    if not tool_ids:
        return 0
    kinds = _trait_kinds()
    retired = 0
    for test in manifest.tests:
        if (
            test.point_id in tool_ids
            and test.status != "orphaned"
            and not test.retired
            and _superseded(test, kinds)
        ):
            test.retired = True
            retired += 1
    if retired:
        save_manifest(manifest, manifest_file)
    return retired


def _apply_trait_lifecycle(manifest: Manifest, changeset: Changeset) -> tuple[int, int]:
    """Retire the tests of traits that stopped applying; restore returning ones.

    Only tools this run observed are touched (``traits_planned`` holds exactly
    those), so an unreachable server's held tests stay as they were. Returns
    ``(retired, restored)``. Neither touches a file: retirement is a manifest
    statement, and the stub stays on disk exactly as it was.

    A per-tool stub for a trait that is now an engine trait (see
    :func:`_superseded`) is retired even though its trait applies — the engine
    module runs that trait — and is never restored.
    """
    retired = restored = 0
    if not changeset.traits_planned:
        return retired, restored
    kinds = _trait_kinds()
    for test in manifest.tests:
        planned = changeset.traits_planned.get(test.point_id)
        if planned is None or test.trait is None or test.status == "orphaned":
            continue
        applies = test.trait in planned and not _superseded(test, kinds)
        if applies and test.retired:
            test.retired = False
            restored += 1
        elif not applies and not test.retired:
            test.retired = True
            retired += 1
    return retired, restored


def _plan_stubs(manifest: Manifest, changeset: Changeset) -> list[_PendingStub]:
    """The stubs this sync will write.

    A detected point gets one when it is new. A declared tool gets one per
    applicable **generated** trait that has no test yet — whether the tool is
    new or the trait newly applies to it. An engine trait gets none: the engine
    runs it from the manifest. The trait table is loaded lazily and only when a
    declared tool is present, so a project with no declarations never reads it.
    """
    pending: list[_PendingStub] = []
    for point in changeset.new_points:
        if point.type != _TOOL_POINT_TYPE:
            pending.append(
                _PendingStub(point, None, stubgen.stub_file_for(point), _DEFAULT_TIER)
            )

    tools = [p for p in changeset.detected_points if p.id in changeset.traits_planned]
    if not tools:
        return pending

    from itest.core.declarations.traits import TraitTableError, load_traits

    table = load_traits()
    # A binding (or a P30 per-trait stub) covers a generated trait; an engine
    # case never does — it is a different mechanism for a different kind.
    covered = {
        (t.point_id, t.trait)
        for t in manifest.tests
        if t.trait is not None and not _is_engine_case(t)
    }
    for point in tools:
        for trait_id in changeset.traits_planned[point.id]:
            trait = table.get(trait_id)
            if trait is None:
                raise TraitTableError(
                    f"plan.json gives {point.source}/{point.target} trait "
                    f"{trait_id!r}, which the trait table does not define. "
                    "Re-run `itest plan`."
                )
            if trait.kind != "generated" or (point.id, trait_id) in covered:
                continue
            pending.append(
                _PendingStub(
                    point,
                    trait,
                    stubgen.tool_stub_file_for(point, trait.tier),
                    trait.tier,
                )
            )
    return pending


def _is_engine_case(test: TestEntry) -> bool:
    return test.test_name.startswith(f"{stubgen.ENGINE_TEST}[")


def _write_tool_support(
    manifest: Manifest, changeset: Changeset, base_dir: Path
) -> int:
    """Each observed server's engine modules and its conftest; register cases.

    An **engine module** (one per server and group — active, or passive for
    every other tier — that has an engine trait) is
    ITest-owned: written when absent, rewritten when it no longer matches the
    template *and* still matches its recorded ownership hash, and otherwise —
    a human edited it — left frozen, which ``_generate_stubs`` then reports.
    Every (tool, engine trait) it will run is registered in the manifest under
    the parametrized node name, so verify maps each result to its point.

    The **conftest** is human-owned from birth: written if absent, never
    rewritten, never registered, never hashed.

    Returns the number of engine cases newly registered.
    """
    if not changeset.traits_planned:
        return 0
    from itest.core.declarations.traits import load_traits

    table = load_traits()
    tools = [p for p in changeset.detected_points if p.id in changeset.traits_planned]

    cases: dict[tuple[str, str], list[tuple[IntegrationPoint, str]]] = {}
    for point in tools:
        for trait_id in changeset.traits_planned[point.id]:
            trait = table.get(trait_id)
            if trait is not None and trait.kind == "engine":
                # Keyed by the module a case lands in, not by its tier: every
                # non-active tier shares one module, and keying by tier would
                # render that one file once per tier, each overwriting the last.
                group = stubgen.engine_group(trait.tier)
                cases.setdefault((point.source, group), []).append((point, trait_id))

    registered = 0
    for (server, group), pairs in sorted(cases.items()):
        file_rel = stubgen.engine_module_for(server, group)
        file_abs = stubgen.stub_file_path(base_dir, file_rel)
        expected = stubgen.render_engine_module(server, group)
        recorded = {t.ownership_hash for t in manifest.tests if t.path == file_rel}
        # ITest's while it is absent, unrecorded, or still what ITest wrote.
        owned = (
            not file_abs.exists()
            or not recorded
            or stubgen.file_hash(file_abs) in recorded
        )
        if owned and (
            not file_abs.exists() or file_abs.read_text(encoding="utf-8") != expected
        ):
            file_abs.parent.mkdir(parents=True, exist_ok=True)
            file_abs.write_text(expected, encoding="utf-8")
        current = stubgen.file_hash(file_abs)
        # A frozen (hand-edited) module keeps its recorded hash, so it goes on
        # being reported; an ITest-owned one records what is now on disk.
        ownership = current if owned else next(iter(recorded))

        names = {t.test_name for t in manifest.tests if t.path == file_rel}
        for point, trait_id in pairs:
            name = stubgen.engine_test_name(point, trait_id)
            if name in names:
                continue
            manifest.tests.append(
                TestEntry(
                    id=f"e-{point.id}-{trait_id.lower()}",
                    point_id=point.id,
                    path=file_rel,
                    test_name=name,
                    trait=trait_id,
                    ownership_hash=ownership,
                    status="implemented",
                    tier=table.get(trait_id).tier,
                    resource_group=point.target,
                )
            )
            names.add(name)
            registered += 1
        if owned:
            for test in manifest.tests:
                if test.path == file_rel:
                    test.ownership_hash = current

    generated = [t for t in table.traits if t.kind == "generated"]
    for server in sorted({p.source for p in tools}):
        conftest = stubgen.stub_file_path(base_dir, stubgen.conftest_for(server))
        if not conftest.exists():
            conftest.parent.mkdir(parents=True, exist_ok=True)
            conftest.write_text(
                stubgen.render_conftest(server, generated), encoding="utf-8"
            )
    return registered


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
    point_targets = {p.id: p.target for p in changeset.detected_points}

    routed: dict[str, list[_PendingStub]] = {}
    for stub in _plan_stubs(manifest, changeset):
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
        stubgen.append_stubs(file_abs, blocks, header=new_stubs[0].header)
        final_hash = stubgen.file_hash(file_abs)

        for stub, name in pending:
            manifest.tests.append(
                TestEntry(
                    id=stub.entry_id(),
                    point_id=stub.point.id,
                    path=file_rel,
                    test_name=name,
                    trait=stub.trait.id if stub.trait is not None else None,
                    ownership_hash=final_hash,
                    # A binding carries no skip line: its body is complete,
                    # and what it still lacks (a fixture, the check library)
                    # shows up as a skip at run time, not as a stub status.
                    status="stub" if stub.trait is None else "implemented",
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
