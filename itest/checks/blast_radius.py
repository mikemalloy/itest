"""Family B, Blast radius: what does calling this tool do?

B1 here is the **agreement** check — readonly, from the listing, never calling
the tool. Its active-tier sibling, "B1 observed" (call a read-classified tool
with a sentinel and look at the store afterwards), is not built yet.
"""

from __future__ import annotations

import dataclasses

from itest.checks._base import (
    CheckResult,
    ListingUnavailable,
    attributes_of,
    engine_check,
    find_tool,
    not_verifiable,
    reference_listing,
    server_of,
    tool_of,
)
from itest.probes.mcp import McpTarget, ToolInfo, classify_mutation

#: Manifest provenance values that mean the declaration stated the class.
_DECLARED = frozenset({"confirmed", "declared"})


def _hint(annotations: dict) -> str | None:
    """The annotation that decided, rendered for a reviewer."""
    if annotations.get("destructiveHint") is True:
        return "destructiveHint"
    if annotations.get("readOnlyHint") is True:
        return "readOnlyHint"
    if annotations.get("readOnlyHint") is False:
        return "readOnlyHint=false"
    if annotations.get("destructiveHint") is False:
        return "destructiveHint=false"
    return None


def _name_class(info: ToolInfo) -> str | None:
    """What the name alone says: the classifier run with the annotations removed."""
    cls, source = classify_mutation(dataclasses.replace(info, annotations={}))
    return cls if source == "name" else None


def _name_pattern(name: str) -> str:
    prefix, _, rest = name.partition("_")
    return f"{prefix}_*" if rest else name


def _says(cls: str) -> str:
    return "read-only" if cls == "read" else cls


@engine_check("B1")
def check_b1(point: dict, target: McpTarget, *, authenticated: bool) -> CheckResult:
    """B1 mutation class (agreement): do the statements about this tool agree?

    Compares three statements without calling the tool: the manifest's
    ``mutation`` / ``mutation_source``, a fresh ``classify_mutation`` of the live
    listing (the server's annotation, and the name heuristic on its own), and the
    declaration — which the manifest records as provenance ``confirmed`` or
    ``declared`` when it stated a class.

    - ``pass`` — every statement that exists agrees. The detail names them
      ("annotation destructiveHint; name delete_*; declared destructive").
    - ``changed`` — the live class differs from the manifest's: the class moved
      since the last sync. Sync turns this into drift.
    - ``fail`` — the annotation and the name disagree and no declaration settles
      it ("annotation says read-only, name says create_*; declare it").
    - ``not_verifiable`` — nothing classifies the tool (``unknown``), only the
      declaration states a class, the tool is not listed, or no listing.

    **The limit, by design.** Every input here is a *statement*. A tool whose
    annotation and name agree and are both false looks consistent, and this check
    passes it: ``lookalike_read`` in ``examples/reference-mcp/`` is annotated
    read-only, named like a read, and mutates. An annotation lie is visible only
    by observing behaviour — calling a read-classified tool and looking at the
    store — which is the active-tier "B1 observed" check, not this one.

    Standards: OWASP Agentic Top 10 ASI02 (Tool Misuse); the OWASP Agent Control
    Standard's AgBOM mutation attribute. Semgrep cheatsheet: no row mapped (the
    cheatsheet's server-tab annotation rows describe what a server should emit,
    not a cross-check of it).
    """
    server, tool = server_of(point), tool_of(point)
    attributes = attributes_of(point)
    recorded = str(attributes.get("mutation") or "unknown")
    provenance = str(attributes.get("mutation_source") or "detected")
    declared = recorded if provenance in _DECLARED else None

    try:
        tools = reference_listing(target, authenticated=authenticated)
    except ListingUnavailable as exc:
        return not_verifiable(str(exc), {"server": server, "tool": tool})
    info = find_tool(tools, tool)
    if info is None:
        return not_verifiable(
            f"{tool!r} is not in tools/list, so its class cannot be read "
            "(D1 reports the orphan)",
            {"server": server, "tool": tool},
        )

    live, live_source = classify_mutation(info)
    hint = _hint(info.annotations)
    name_class = _name_class(info)
    evidence = {
        "server": server,
        "tool": tool,
        "manifest": {"mutation": recorded, "mutation_source": provenance},
        "live": {"class": live, "source": live_source},
        "annotations": dict(info.annotations),
        "name_class": name_class,
        "declared": declared,
    }

    conflict = live_source.startswith("conflict:")
    sources = []
    if hint is not None:
        sources.append(f"annotation {hint}")
    if name_class is not None:
        says = f" says {name_class}" if conflict else ""
        sources.append(f"name {_name_pattern(tool)}{says}")
    if declared is not None:
        settles = " settles it" if conflict else ""
        sources.append(f"declared {declared}{settles}")
    named = "; ".join(sources)

    if live == "unknown":
        if provenance == "detected" and recorded != "unknown":
            return CheckResult(
                "changed",
                f"class moved since the last sync: recorded {recorded}, the live "
                "listing no longer classifies it (no annotation, no name signal)",
                evidence,
            )
        if declared is not None:
            return not_verifiable(
                f"only the declaration states a class (declared {declared}); the "
                "server's annotations and name say nothing to agree with",
                evidence,
            )
        return not_verifiable(
            "mutation class unknown: no annotation, and the name suggests nothing",
            evidence,
        )

    if conflict and provenance != "confirmed":
        return CheckResult(
            "fail",
            f"annotation says {_says(live)}, name says {_name_pattern(tool)}; "
            "declare it",
            evidence,
        )

    if live != recorded:
        return CheckResult(
            "changed",
            f"class moved since the last sync: recorded {recorded} ({provenance}), "
            f"live {live} ({named})",
            evidence,
        )

    return CheckResult("pass", f"{live}: {named}", evidence)
