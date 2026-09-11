"""What ``itest traits`` and ``itest recipes`` say: the table, one tool, recipes.

Render functions in the house style (see DESIGN.md, Presentation contract): each
returns the canonical text, and the CLI's style layer only colours it. Each has
a JSON twin for ``--json``. Nothing here contacts a server — the table is read
through the declarations loader, a tool's attributes from the manifest — and
nothing here imports the probe, so either command runs against a checkout whose
servers are all down.
"""

from __future__ import annotations

from pathlib import Path

from itest.core.declarations.traits import (
    Trait,
    TraitDecision,
    TraitTable,
    trait_table_hash,
)
from itest.core.manifest import IntegrationPoint, Manifest

#: Where the bundled skill keeps its recipes, relative to a skill root.
RECIPES_REL = Path("itest-implementer") / "references" / "recipes"

_DECISION_WIDTH = len("does not apply") + 3


def _columns(rows: list[list[str]], indent: str = "  ") -> list[str]:
    """Left-aligned columns, two spaces apart, no trailing whitespace."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        (
            indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        ).rstrip()
        for row in rows
    ]


# --- the table ------------------------------------------------------------------


def _standards(trait: Trait) -> str:
    """The published ids a trait answers, or a dash: no mapping is not a gap
    to be filled with a guess."""
    return ", ".join(trait.standards) or "—"


def table_json(table: TraitTable) -> dict:
    return {
        "trait_table_hash": trait_table_hash(table),
        "families": dict(table.families),
        "traits": [
            {
                "id": t.id,
                "code": t.code,
                "family": t.family,
                "family_name": table.families.get(t.family, t.family),
                "name": t.name,
                "kind": t.kind,
                "tier": t.tier,
                "applies_when": t.applies_when,
                "standards": list(t.standards),
                "recipe": t.recipe,
            }
            for t in table.traits
        ],
    }


def render_table(table: TraitTable) -> str:
    """The table: the slug is the identity, the code a short label beside it,
    and the standards the published ids each trait answers."""
    kinds = [t.kind for t in table.traits]
    out = [
        f"Trait table {trait_table_hash(table)}: {len(table.traits)} traits in "
        f"{len(table.families)} families ({kinds.count('engine')} engine, "
        f"{kinds.count('generated')} generated).",
        "",
    ]
    rows = [
        [
            "SLUG",
            "CODE",
            "FAMILY",
            "KIND",
            "TIER",
            "APPLIES WHEN",
            "STANDARDS",
            "RECIPE",
        ]
    ]
    rows += [
        [
            t.id,
            t.code,
            table.families.get(t.family, t.family),
            t.kind,
            t.tier,
            t.applies_when,
            _standards(t),
            t.recipe,
        ]
        for t in table.traits
    ]
    out += _columns(rows)
    return "\n".join(out)


# --- one tool -------------------------------------------------------------------


class ToolLookupError(Exception):
    """A ``--for`` that names no tool the manifest knows. The message says which
    servers or tools do exist."""


def find_tool(manifest: Manifest, address: str) -> IntegrationPoint:
    """The declared tool at ``<server>/<tool>``, or :class:`ToolLookupError`."""
    server, _, tool = address.partition("/")
    if not server or not tool:
        raise ToolLookupError(
            f"--for takes <server>/<tool>, e.g. reference-mcp/delete_record; "
            f"got {address!r}."
        )
    tools = [p for p in manifest.points if p.type == "mcp_tool"]
    servers = sorted({p.source for p in tools})
    if server not in servers:
        raise ToolLookupError(
            f"No declared server {server!r} in the manifest. Known servers: "
            f"{', '.join(servers) or '(none — declare one and run itest sync)'}."
        )
    for point in tools:
        if point.source == server and point.target == tool:
            return point
    known = sorted(p.target for p in tools if p.source == server)
    raise ToolLookupError(
        f"{server} has no tool {tool!r} in the manifest. Known tools on {server}: "
        f"{', '.join(known)}."
    )


def tool_json(
    point: IntegrationPoint,
    decisions: list[TraitDecision],
    table: TraitTable,
    manifest: Manifest,
) -> dict:
    current = trait_table_hash(table)
    return {
        "server": point.source,
        "tool": point.target,
        "point_id": point.id,
        "mutation": point.attributes.get("mutation"),
        "mutation_source": point.attributes.get("mutation_source"),
        "trait_table_hash": current,
        "table_changed_since_sync": manifest.trait_table_hash not in (None, current),
        "traits_planned": point.traits_planned,
        "traits": [
            {
                "id": d.trait.id,
                "code": d.trait.code,
                "name": d.trait.name,
                "kind": d.trait.kind,
                "tier": d.trait.tier,
                "applies": d.applies,
                "reason": d.reason,
                "standards": list(d.trait.standards),
            }
            for d in decisions
        ],
    }


def render_tool(
    point: IntegrationPoint,
    decisions: list[TraitDecision],
    table: TraitTable,
    manifest: Manifest,
    tag: str,
) -> str:
    current = trait_table_hash(table)
    applying = sum(1 for d in decisions if d.applies)
    out = [
        f"{point.source}/{point.target}: {tag}",
        f"  id={point.id}  declared in {point.hcl_address}",
        f"  {applying} of {len(decisions)} traits apply (table {current}), "
        "decided from the manifest's recorded attributes.",
    ]
    if manifest.trait_table_hash not in (None, current):
        out.append(
            f"  The table changed since the last sync ({manifest.trait_table_hash} "
            f"→ {current}): run `itest sync` to apply it."
        )
    out.append("")
    rows = [
        [
            ("APPLIES" if d.applies else "does not apply").ljust(_DECISION_WIDTH - 2),
            d.trait.id,
            d.trait.code,
            d.trait.name,
            d.trait.kind,
            d.trait.tier,
            d.reason,
            _standards(d.trait),
        ]
        for d in decisions
    ]
    out += _columns(rows)
    return "\n".join(out)


# --- recipes --------------------------------------------------------------------


def recipe_search_path(home: Path) -> list[Path]:
    """Where the bundled skill's recipes are looked for, in order.

    The project's own copy of the skill (a checkout of ITest, or a vendored
    skill) first, then a project-local Claude skill install, then a global one.
    """
    return [
        Path("skills") / RECIPES_REL,
        Path(".claude") / "skills" / RECIPES_REL,
        home / ".claude" / "skills" / RECIPES_REL,
    ]


def resolve_recipes_dir(explicit: Path | None, base_dir: Path, home: Path) -> Path:
    """``explicit`` if given; else the first search-path entry that exists; else
    the first entry, so the report still names where it looked."""
    if explicit is not None:
        return explicit
    candidates = recipe_search_path(home)
    for candidate in candidates:
        if (base_dir / candidate).is_dir():
            return candidate
    return candidates[0]


def recipes_json(table: TraitTable, recipes_dir: Path, base_dir: Path) -> dict:
    referenced: dict[str, list[str]] = {}
    for trait in table.traits:
        referenced.setdefault(trait.recipe, []).append(trait.id)
    return {
        "recipes_dir": str(recipes_dir),
        "recipes": [
            {
                "recipe": name,
                "path": str(recipes_dir / name),
                "exists": (base_dir / recipes_dir / name).is_file(),
                "traits": traits,
            }
            for name, traits in referenced.items()
        ],
    }


def render_recipes(table: TraitTable, recipes_dir: Path, base_dir: Path) -> str:
    """Every recipe the table names. A missing file is a line, never an error:
    the recipes are the skill's, and may land after the table does."""
    document = recipes_json(table, recipes_dir, base_dir)
    found = "" if (base_dir / recipes_dir).is_dir() else " (directory not found)"
    out = [f"Recipes the trait table references, in {recipes_dir}{found}:", ""]
    rows = [
        [
            "present" if r["exists"] else "missing",
            r["path"],
            ", ".join(r["traits"]),
        ]
        for r in document["recipes"]
    ]
    out += _columns(rows)
    present = sum(1 for r in document["recipes"] if r["exists"])
    total = len(document["recipes"])
    out += ["", f"{total} recipe(s): {present} present, {total - present} missing."]
    return "\n".join(out)
