"""Turn a declared server's live tool listing into ``mcp_tool`` integration points.

This is the bridge, and the only module in ``itest/core`` that reaches for a
probe. The separation it has to respect is in DESIGN.md: a detector reads
Terraform and never touches the network; a probe touches the network and never
reads Terraform. A declaration is neither — it is a *fact file*, and the points
it yields need the server's own answer to "which tools do you have?", which only
the probe can get. So this module reads the declaration, asks the probe, and
builds points; the planner calls it and knows nothing about transports.

Three decisions are recorded here because the code is the only place they are
enforced:

**Identity is (server, tool).** A point id is
``hash("mcp_tool", server, name)`` and nothing else. ``schema_hash`` and
``description_hash`` are *attributes*: a server that rewrites a tool's
description has not replaced the tool, so the id must not move — otherwise every
wording change would orphan the tests that cover it and re-stub them empty,
which is precisely the drift nobody would then notice.

**The mutation class is detected and the declaration is cross-checked.** The
developer never states the dangerous field; they may state a belief, and a
disagreement with the live listing **refuses generation** (see
:class:`MutationConflict`) rather than picking a winner. One of the two is wrong,
and nothing here can know which. Provenance is recorded per point:
``detected`` (the declaration said ``detect``), ``confirmed`` (it agreed), or
``declared`` (the server said nothing and the name suggested nothing, so the
declaration filled the gap).

**The attributes carry what the applies-when table asks about.** The table
(``itest/traits/traits.yaml``) addresses ``auth.second_tenant_env``,
``audit.sink`` and ``identity.runs_as``, which are facts about the *server*, so
they are copied onto each of its points. ``has_free_form_input`` is derived
from the input schema here, because the schema itself is not kept — only its
hash — and sync has nothing else to read.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from itest.core.declarations.loader import declaration_path, resolve_url
from itest.core.declarations.schema import NO_TRAITS, Declaration, Egress
from itest.core.declarations.traits import TraitTable
from itest.core.manifest import IntegrationPoint
from itest.probes.mcp import McpTarget, ToolInfo, classify_mutation

#: The point type declarations produce. One per (server, tool).
POINT_TYPE = "mcp_tool"

#: What ``classify_mutation`` returns when neither the annotations nor the name
#: decided. Not a disagreement with anything — an absence of detection.
UNKNOWN = "unknown"


class MutationConflict(Exception):
    """A declared mutation class that the live server contradicts.

    Raised during planning, before anything is written. The message names the
    tool, the detected class and what decided it, and the declared class.
    """


class DeclaredTraitUnknown(Exception):
    """A per-tool ``traits:`` override naming a trait the table cannot place."""


def tool_point_id(server: str, name: str) -> str:
    """The point id for one tool: a function of the server and the tool name.

    Deliberately not of the schema or the description. Those are attributes, and
    a point that changed its id whenever a server reworded a docstring would
    orphan its own tests.
    """
    parts = [POINT_TYPE, server, name]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def build_target(
    declaration: Declaration, base_dir: Path | None = None
) -> McpTarget | None:
    """The probe target for a declaration, or ``None`` when it cannot be built.

    ``None`` means an ``http`` server whose ``url_env`` is unset: the caller
    reports it as unreachable and carries on. Nothing here logs the url.
    """
    transport = declaration.transport
    credential_env = declaration.auth.credential_env
    if transport.kind == "stdio":
        return McpTarget(
            kind="stdio",
            command=list(transport.command or []),
            credential_env=credential_env,
        )

    url = resolve_url(declaration, base_dir)
    if not url:
        return None
    return McpTarget(kind="http", url=url, credential_env=credential_env)


def has_free_form_input(input_schema: dict[str, Any] | None) -> bool:
    """True when any string parameter is unconstrained.

    "Unconstrained" means a string with no ``enum``, no ``pattern`` and no
    ``maxLength`` — the shape that lets a caller put an expression, a path or a
    query language where a name was expected. It is deliberately a narrow,
    mechanical reading of the declared schema: the point is to mark the tools
    worth probing for passthrough, not to judge them.
    """
    properties = (input_schema or {}).get("properties") or {}
    if not isinstance(properties, dict):
        return False
    for spec in properties.values():
        if not isinstance(spec, dict):
            continue
        declared_type = spec.get("type")
        is_string = declared_type == "string" or (
            isinstance(declared_type, list) and "string" in declared_type
        )
        if not is_string:
            continue
        if not any(key in spec for key in ("enum", "pattern", "maxLength")):
            return True
    return False


def resolve_mutation(
    server: str, tool: str, declared: str, detected: str, detected_source: str
) -> tuple[str, str]:
    """Return ``(mutation_class, provenance)`` or raise :class:`MutationConflict`.

    The whole rule, in one place:

    - ``detect`` (the default): detection wins, provenance ``detected``.
    - a declared class that matches: provenance ``confirmed``.
    - a declared class where detection found nothing at all: the declaration
      fills the gap, provenance ``declared``.
    - a declared class that contradicts detection: refused.

    Never a silent default: every point leaves here with a provenance that says
    who decided.
    """
    if declared == "detect":
        return detected, "detected"
    if detected == UNKNOWN:
        # The server declared no annotations and the name suggested nothing.
        # A declaration is the only statement there is, so it stands — and says
        # so in its provenance.
        return declared, "declared"
    if declared == detected:
        return detected, "confirmed"
    raise MutationConflict(
        f"Declared mutation class disagrees with the live server.\n"
        f"  server:   {server}\n"
        f"  tool:     {tool}\n"
        f"  detected: {detected} (source: {detected_source})\n"
        f"  declared: {declared} (in {declaration_path(server)})\n"
        "Nothing was generated and no manifest was written: one of the two is "
        "wrong, and ITest cannot know which. Fix the declaration, fix the "
        "server's annotation, or write `mutation: detect` to take the detected "
        "class."
    )


def _egress_attribute(value: Egress | str | None) -> dict[str, Any] | None:
    """The JSON-ready form of a resolved egress value. ``none`` becomes ``None``."""
    if value is None or value == "none":
        return None
    return value.model_dump(mode="json")


def _resolved_egress(declaration: Declaration, tool: str) -> Egress | str | None:
    """The egress value for one tool: its own statement, else the server's.

    A tool's ``None`` is *silence* — inherit. Its ``"none"`` is a statement that
    this tool egresses nothing even though the server-wide default says
    otherwise, and the two must not be collapsed.
    """
    override = declaration.tools.get(tool)
    if override is not None and override.egress is not None:
        return override.egress
    return declaration.defaults.egress


def _resolved_approval(declaration: Declaration, tool: str) -> str:
    override = declaration.tools.get(tool)
    if override is not None and override.approval is not None:
        return override.approval
    return declaration.approval.destructive_requires


def _resolved_active(declaration: Declaration, tool: str) -> bool:
    override = declaration.tools.get(tool)
    if override is not None and override.active is not None:
        return override.active
    return declaration.defaults.active


def _declared_mutation(declaration: Declaration, tool: str) -> str:
    override = declaration.tools.get(tool)
    if override is not None and override.mutation is not None:
        return override.mutation
    return declaration.defaults.mutation


def build_points(
    declaration: Declaration,
    tools: list[ToolInfo],
    table: TraitTable,
) -> list[IntegrationPoint]:
    """One ``mcp_tool`` point per live tool, cross-checked against the declaration.

    Raises :class:`MutationConflict` on a disagreement and
    :class:`DeclaredTraitUnknown` on a ``traits:`` override the table cannot
    place. Both are refusals at plan time, before anything is written.
    """
    now = datetime.now(UTC)
    server = declaration.server
    points: list[IntegrationPoint] = []

    for tool in tools:
        detected, detected_source = classify_mutation(tool)
        mutation, provenance = resolve_mutation(
            server,
            tool.name,
            _declared_mutation(declaration, tool.name),
            detected,
            detected_source,
        )
        override = declaration.tools.get(tool.name)
        declared_traits = override.traits if override is not None else None
        if declared_traits:
            for trait_id in declared_traits:
                if trait_id != NO_TRAITS and table.get(trait_id) is None:
                    raise DeclaredTraitUnknown(
                        f"{declaration_path(server)} gives tool "
                        f"{tool.name!r} the trait {trait_id!r}, which the trait "
                        f"table does not define. Known traits: "
                        f"{', '.join(table.ids)}."
                    )

        points.append(
            IntegrationPoint(
                id=tool_point_id(server, tool.name),
                type=POINT_TYPE,
                source=server,
                target=tool.name,
                attributes={
                    "mutation": mutation,
                    "mutation_source": provenance,
                    "egress": _egress_attribute(
                        _resolved_egress(declaration, tool.name)
                    ),
                    "approval": _resolved_approval(declaration, tool.name),
                    "active": _resolved_active(declaration, tool.name),
                    "schema_hash": tool.schema_hash,
                    "description_hash": tool.description_hash,
                    "annotations": dict(tool.annotations),
                    # Derived here because the schema itself is not kept, only
                    # its hash, and the applies-when table asks about it.
                    "has_free_form_input": has_free_form_input(tool.input_schema),
                    # Three facts about the server, carried onto its points so the
                    # table can address them without reloading the declaration.
                    "second_tenant_env": declaration.auth.second_tenant_env,
                    "audit_sink": declaration.audit.sink,
                    "runs_as": declaration.identity.runs_as,
                    "traits": list(declared_traits) if declared_traits else None,
                },
                hcl_address=declaration_path(server),
                origin="declared",
                first_seen=now,
                last_seen=now,
            )
        )
    return points


def orphaned_overrides(declaration: Declaration, live: list[ToolInfo]) -> list[str]:
    """Tools the declaration overrides that the server does not list.

    Flagged, never deleted: the override may be a rename the server has not
    deployed yet, or a tool that was withdrawn — and which of those it is, is not
    this tool's call.
    """
    live_names = {tool.name for tool in live}
    return [name for name in declaration.tools if name not in live_names]


def trait_context(point: IntegrationPoint) -> dict[str, Any]:
    """The attribute names the applies-when table evaluates against.

    Built from the point alone: sync reads the manifest and the changeset, never
    the declaration, so everything the table asks about has to be on the point.
    """
    attributes = point.attributes
    return {
        "mutation": attributes.get("mutation"),
        "egress": attributes.get("egress"),
        "approval": attributes.get("approval"),
        "active": attributes.get("active"),
        "has_free_form_input": attributes.get("has_free_form_input"),
        "auth.second_tenant_env": attributes.get("second_tenant_env"),
        "audit.sink": attributes.get("audit_sink"),
        "identity.runs_as": attributes.get("runs_as"),
    }
