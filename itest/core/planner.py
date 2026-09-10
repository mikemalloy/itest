"""The ``itest plan`` engine.

Reads a ``terraform show -json`` document, detects integration points, diffs
them against the existing manifest, and writes a proposed changeset plus a
Mermaid diagram. Planning is strictly read-only with respect to the manifest
and any test files.

**Declared servers are planned alongside the detected ones.** An MCP server is
not in Terraform state, so nothing can infer it; a declaration under
`.itest/tools/` is the person who runs it saying it exists, and planning it means
asking the server for its tool list. That is the one place plan reaches past the
filesystem, and it is deliberately narrow:

- the probe is imported *inside* the declaration step, so a project with no
  declarations never loads a transport at all;
- a server that cannot be reached — an unset url variable, a command that is not
  an MCP server — is a line in the changeset, never an exception. One broken
  server must not blind the rest of the project;
- a declared mutation class the live listing contradicts **aborts the whole
  plan** before a single artifact is written. Nothing is generated from a
  statement ITest cannot trust.

Three changeset sections exist only for declared points, and all three are
append-only in the rendering, so a project without declarations prints exactly
what it always did: ``changed`` (an attribute drifted; the id did not move),
``orphaned_tool_overrides`` (the declaration describes a tool the server no
longer lists) and ``unreachable_servers``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from itest.core import points as point_labels
from itest.core.detectors.base import detect_all
from itest.core.manifest import IntegrationPoint, TestEntry, load_manifest
from itest.core.mermaid import generate_mermaid

ITEST_DIR = ".itest"
MANIFEST_NAME = "manifest.yaml"
PLAN_NAME = "plan.json"
DIAGRAM_NAME = "diagram.mmd"

#: Ceiling on `terraform show -json` (seconds). Reading state is normally quick;
#: this only catches a wedged terraform so it cannot block the CLI forever.
_TERRAFORM_TIMEOUT = 120.0


class PlanInputError(Exception):
    """Raised when plan JSON cannot be obtained (bad file or terraform error)."""


class OrphanedOverride(BaseModel):
    """A declaration describing a tool its server does not list.

    Flagged, never deleted: it may be a rename the server has not deployed yet,
    or a tool that was withdrawn, and which of those it is is not ITest's call.
    """

    server: str
    tool: str
    declaration: str


class Changeset(BaseModel):
    """The proposed change relative to the current manifest."""

    new_points: list[IntegrationPoint] = Field(default_factory=list)
    unchanged_points: list[IntegrationPoint] = Field(default_factory=list)
    resurrected_points: list[IntegrationPoint] = Field(default_factory=list)
    #: Points whose id is already known but whose drift attributes moved — a
    #: reworded tool description, a changed input schema. The same point,
    #: differently described, and that is a finding rather than a new point.
    changed_points: list[IntegrationPoint] = Field(default_factory=list)
    orphan_candidates: list[TestEntry] = Field(default_factory=list)
    unanalyzed: dict[str, int] = Field(default_factory=dict)
    #: Declared tools their server no longer lists.
    orphaned_tool_overrides: list[OrphanedOverride] = Field(default_factory=list)
    #: Declared servers that could not be asked, by server name.
    unreachable_servers: dict[str, str] = Field(default_factory=dict)
    #: For each changed point, which attributes moved. The values themselves are
    #: hashes and mean nothing to a reader; *which* one moved is the finding —
    #: a tool that rewrote what it says it does without changing what it accepts
    #: moves exactly one of them.
    changed_attributes: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def detected_points(self) -> list[IntegrationPoint]:
        """Every point this run saw (new + unchanged + resurrected + changed).

        ``changed`` belongs here: the registry must record the point's *new*
        attributes, and leaving it out would quietly revert the drift.
        """
        return (
            self.new_points
            + self.unchanged_points
            + self.resurrected_points
            + self.changed_points
        )


def manifest_path(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / MANIFEST_NAME


def plan_path(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / PLAN_NAME


def diagram_path(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / DIAGRAM_NAME


PLAN_ROOT_KEYS = ("planned_values", "values")


def _validate_root(document: object, origin: str) -> dict:
    """Ensure the document carries a plan or state root.

    ``terraform show -json tfplan`` emits plan JSON (``planned_values``);
    ``terraform show -json`` with no plan file emits state JSON (``values``).
    Both are accepted transparently here, once, so every detector benefits.
    """
    if isinstance(document, dict) and any(k in document for k in PLAN_ROOT_KEYS):
        return document
    if isinstance(document, dict) and set(document) <= {
        "format_version",
        "terraform_version",
    }:
        # Exactly what terraform emits for a workspace with no state.
        raise PlanInputError(
            f"{origin} is an empty state: Terraform reports nothing deployed "
            "here. Either nothing has been applied from this directory or "
            "workspace, or the backend holding the state is not the one "
            "configured in this checkout (`terraform init`, then check "
            "`terraform workspace show` and `terraform state list`)."
        )
    raise PlanInputError(
        f"{origin} is neither Terraform plan nor state JSON: expected a "
        f"top-level {PLAN_ROOT_KEYS[0]!r} (plan) or {PLAN_ROOT_KEYS[1]!r} "
        "(state) key. Produce it with `terraform show -json [PLANFILE]`."
    )


def load_plan_json(tf_json: Path | None, base_dir: Path) -> dict:
    """Obtain the terraform plan or state JSON, from a file or from terraform."""
    if tf_json is not None:
        path = Path(tf_json)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PlanInputError(f"--tf-json file not found: {path}") from None
        except json.JSONDecodeError as exc:
            raise PlanInputError(f"--tf-json file is not valid JSON: {exc}") from exc
        return _validate_root(document, f"--tf-json file {path}")

    try:
        proc = subprocess.run(
            ["terraform", "show", "-json"],
            cwd=str(base_dir),
            capture_output=True,
            text=True,
            timeout=_TERRAFORM_TIMEOUT,
        )
    except FileNotFoundError:
        raise PlanInputError(
            "terraform not found on PATH. Pass --tf-json PATH pointing at the "
            "output of `terraform show -json` instead."
        ) from None
    except subprocess.TimeoutExpired as exc:
        raise PlanInputError(
            f"`terraform show -json` did not finish within {_TERRAFORM_TIMEOUT}s "
            "and was killed. Pass --tf-json PATH to supply the JSON directly."
        ) from exc
    if proc.returncode != 0:
        raise PlanInputError(
            "`terraform show -json` failed:\n"
            f"{proc.stderr.strip()}\n"
            "Pass --tf-json PATH to supply the JSON directly."
        )
    try:
        document = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PlanInputError(
            f"`terraform show -json` did not return valid JSON: {exc}"
        ) from exc
    return _validate_root(document, "`terraform show -json` output")


#: Attributes whose change is *drift on an existing point*, never a new point.
#: Only declared tool points carry them, which is why a terraform-only project's
#: changeset is byte-identical to the one it produced before declarations.
DRIFT_ATTRIBUTES = ("schema_hash", "description_hash")


def drifted_attributes(
    point: IntegrationPoint, existing: IntegrationPoint | None
) -> list[str]:
    """Which drift attributes differ from the stored point. Empty when none do."""
    if existing is None:
        return []
    return [
        key
        for key in DRIFT_ATTRIBUTES
        if key in point.attributes
        and point.attributes.get(key) != existing.attributes.get(key)
    ]


def compute_changeset(
    points: list[IntegrationPoint],
    unanalyzed: dict[str, int],
    existing_point_ids: set[str],
    existing_tests: list[TestEntry],
    existing_points: list[IntegrationPoint] | None = None,
) -> Changeset:
    """Diff detected points against what the manifest already knows.

    ``existing_points`` is optional only so the Terraform-only callers that
    predate declarations keep working unchanged: without it, nothing can drift,
    which is exactly right for a point type that carries no drift attributes.
    """
    detected_ids = {p.id for p in points}
    stored = {p.id: p for p in existing_points or []}
    # A detected point whose id matches an orphaned test's point_id is
    # returning, not new: sync drops vanished points from the registry, so the
    # id is unknown here, but the test that covered it still exists on disk and
    # must be re-linked rather than re-stubbed.
    orphaned_point_ids = {t.point_id for t in existing_tests if t.status == "orphaned"}
    resurrected_points = [p for p in points if p.id in orphaned_point_ids]
    resurrected_ids = {p.id for p in resurrected_points}
    new_points = [
        p
        for p in points
        if p.id not in existing_point_ids and p.id not in resurrected_ids
    ]
    known = [
        p for p in points if p.id in existing_point_ids and p.id not in resurrected_ids
    ]
    # A known point whose drift attributes moved is `changed`, not `unchanged`:
    # the id is the same tool, and the description it now carries is not.
    drifted = {p.id: drifted_attributes(p, stored.get(p.id)) for p in known}
    changed_points = [p for p in known if drifted[p.id]]
    changed_ids = {p.id for p in changed_points}
    unchanged_points = [p for p in known if p.id not in changed_ids]
    orphan_candidates = [
        t
        for t in existing_tests
        if t.point_id not in detected_ids and t.status != "orphaned"
    ]
    return Changeset(
        new_points=new_points,
        unchanged_points=unchanged_points,
        resurrected_points=resurrected_points,
        changed_points=changed_points,
        orphan_candidates=orphan_candidates,
        unanalyzed=unanalyzed,
        changed_attributes={pid: names for pid, names in drifted.items() if names},
    )


def plan_declarations(
    base_dir: Path,
) -> tuple[list[IntegrationPoint], list[OrphanedOverride], dict[str, str]]:
    """Ask every declared server for its tools and build its points.

    Returns ``(points, orphaned_overrides, unreachable)``. Raises on a problem
    with a declaration itself — a mutation-class conflict, a trait the table
    cannot place, a malformed file — because those are statements ITest cannot
    act on. A server it merely cannot *reach* is reported, not raised.

    The probe is imported here rather than at module scope: a project with no
    declarations must never load a transport, which is what keeps the read-only
    analysis path free of any route to the network.
    """
    from itest.core.declarations import (
        declaration_path,
        load_declarations,
        missing_url_message,
    )
    from itest.core.declarations import tools as declared_tools
    from itest.core.declarations.traits import load_traits
    from itest.probes.mcp import McpProbeError, list_tools

    declarations = load_declarations(base_dir)
    if not declarations:
        return [], [], {}

    table = load_traits()
    points: list[IntegrationPoint] = []
    orphaned: list[OrphanedOverride] = []
    unreachable: dict[str, str] = {}

    for declaration in declarations:
        target = declared_tools.build_target(declaration, base_dir)
        if target is None:
            # An http server whose url variable is unset. The NAME is safe to
            # print, which is the whole reason the file holds a name.
            unreachable[declaration.server] = missing_url_message(declaration)
            continue
        try:
            live = list_tools(target, base_dir=base_dir)
        except McpProbeError as exc:
            unreachable[declaration.server] = f"unreachable: {exc}"
            continue
        points.extend(declared_tools.build_points(declaration, live, table))
        orphaned.extend(
            OrphanedOverride(
                server=declaration.server,
                tool=name,
                declaration=declaration_path(declaration.server),
            )
            for name in declared_tools.orphaned_overrides(declaration, live)
        )
    return points, orphaned, unreachable


def run_plan(tf_json: Path | None, base_dir: Path) -> Changeset:
    """Full plan flow: detect, diff, and write plan.json + diagram.mmd.

    Does not touch the manifest or any test file. Declared servers are planned
    before anything is written, so a refusal from the cross-check leaves no
    artifact behind at all.
    """
    plan_json = load_plan_json(tf_json, base_dir)
    points, unanalyzed = detect_all(plan_json)
    declared, orphaned_overrides, unreachable = plan_declarations(base_dir)
    points = points + declared

    mpath = manifest_path(base_dir)
    if mpath.exists():
        manifest = load_manifest(mpath)
        existing_ids = {p.id for p in manifest.points}
        existing_tests = manifest.tests
        existing_points = manifest.points
    else:
        existing_ids = set()
        existing_tests = []
        existing_points = []

    changeset = compute_changeset(
        points, unanalyzed, existing_ids, existing_tests, existing_points
    )
    changeset.orphaned_tool_overrides = orphaned_overrides
    changeset.unreachable_servers = unreachable

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
    n_resurrected = len(changeset.resurrected_points)
    n_changed = len(changeset.changed_points)
    n_orphan = len(changeset.orphan_candidates)

    out: list[str] = []
    # Every clause here is append-only: it appears only when something actually
    # happened, so a project with no resurrections and no declared servers prints
    # exactly the line it always printed.
    resurrected_clause = (
        f"{n_resurrected} test(s) resurrected, " if n_resurrected else ""
    )
    changed_clause = f"{n_changed} changed, " if n_changed else ""
    out.append(
        f"ITest plan: {n_new} new, {n_unchanged} unchanged, "
        f"{resurrected_clause}{changed_clause}{n_orphan} orphaned test(s)."
    )
    out.append("")

    if changeset.resurrected_points:
        out.append(f"Resurrected ({n_resurrected}):")
        for p in changeset.resurrected_points:
            out.append(f"  ^ [returning] {p.source} -> {p.target}")
            out.append(f"      id={p.id}  re-linked to its existing test")
        out.append("")

    out.append(f"New integration points ({n_new}):")
    if changeset.new_points:
        for p in changeset.new_points:
            tag = point_labels.summary(p)
            out.append(f"  + [{tag}] {p.source} -> {p.target}")
            out.append(f"      id={p.id}  hcl={p.hcl_address}")
    else:
        out.append("  (none)")
    out.append("")

    # Append-only: a point whose attributes drifted. The id did not move, so
    # nothing is orphaned and nothing is re-stubbed — the description it carries
    # is simply not the one the manifest recorded.
    if changeset.changed_points:
        out.append(f"Changed ({n_changed}):")
        for p in changeset.changed_points:
            tag = point_labels.summary(p)
            out.append(f"  ! [{tag}] {p.source} -> {p.target}")
            moved = ", ".join(changeset.changed_attributes.get(p.id, []))
            out.append(f"      id={p.id}  changed: {moved}")
        out.append("")

    out.append(f"Orphan candidates ({n_orphan}):")
    if changeset.orphan_candidates:
        for t in changeset.orphan_candidates:
            out.append(f"  ~ {t.canonical}  (was point {t.point_id})")
    else:
        out.append("  (none)")
    out.append("")

    # Append-only: a declaration describing a tool its server no longer lists.
    # Reported, never removed from the file — ITest does not edit a declaration.
    if changeset.orphaned_tool_overrides:
        out.append(
            f"Orphaned tool overrides ({len(changeset.orphaned_tool_overrides)}):"
        )
        for override in changeset.orphaned_tool_overrides:
            out.append(f"  ~ {override.server}/{override.tool}")
            out.append(f"      declared in {override.declaration}, not in tools/list")
        out.append("")

    # Append-only: a declared server that could not be asked. A line, never a
    # crash, and never a silent omission.
    if changeset.unreachable_servers:
        out.append(
            f"Unreachable declared servers ({len(changeset.unreachable_servers)}):"
        )
        for server in sorted(changeset.unreachable_servers):
            out.append(f"  ? {server}")
            out.append(f"      {changeset.unreachable_servers[server]}")
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
