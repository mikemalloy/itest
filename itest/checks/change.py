"""Family D, Change: is the tool still there, and is it still what was reviewed?

Three engine checks over one live ``tools/list`` per server per run. None of
them calls a tool.
"""

from __future__ import annotations

from itest.checks._base import (
    CheckResult,
    ListingUnavailable,
    attributes_of,
    engine_check,
    find_tool,
    listing_label,
    manifest_tools,
    not_verifiable,
    reference_listing,
    server_of,
    tool_of,
)
from itest.probes.mcp import McpTarget


@engine_check("change.inventory")
def check_change__inventory(
    point: dict, target: McpTarget, *, authenticated: bool
) -> CheckResult:
    """change.inventory (CHANGE-1): is the tool the manifest records still listed?

    Reads one live ``tools/list`` (cached per server per run) and never calls a
    tool.

    - ``pass`` — the tool is in the listing.
    - ``fail`` — it is not: the manifest point is an orphan. A tool that
      vanished may have been withdrawn, renamed, or hidden from this caller.
    - ``not_verifiable`` — the listing could not be taken (unreachable, refused,
      no credential for an authenticated listing).

    Every change.inventory result for a server carries ``evidence.undeclared``:
    live tools with no manifest point — tools an agent can call that nobody
    reviewed. It is the same list on each result (reported once per server,
    repeated so any one row shows it), ``[]`` when there are none, and ``None``
    when there is no manifest to compare against.

    Standards: OWASP Agentic Top 10 ASI04 (Agentic Supply Chain); the Semgrep
    MCP security cheatsheet, client tab, row 14 (name collisions — a newly
    appearing, unreviewed tool is where a colliding name comes from).
    """
    server, tool = server_of(point), tool_of(point)
    listing = listing_label(authenticated)
    try:
        tools = reference_listing(target, authenticated=authenticated)
    except ListingUnavailable as exc:
        return not_verifiable(
            str(exc), {"server": server, "tool": tool, "listing": listing}
        )

    recorded = manifest_tools(server)
    undeclared = (
        None if recorded is None else sorted({t.name for t in tools} - set(recorded))
    )
    evidence = {
        "server": server,
        "tool": tool,
        "listed": find_tool(tools, tool) is not None,
        "undeclared": undeclared,
        "listing": listing,
    }
    if evidence["listed"]:
        return CheckResult("pass", f"{tool!r} is listed by {server}", evidence)
    return CheckResult("fail", f"{tool!r} is not in tools/list; orphan", evidence)


def _hash_check(
    field: str, what: str, point: dict, target: McpTarget, authenticated: bool
) -> CheckResult:
    server, tool = server_of(point), tool_of(point)
    recorded = attributes_of(point).get(field)
    base = {"server": server, "tool": tool, "listing": listing_label(authenticated)}
    try:
        tools = reference_listing(target, authenticated=authenticated)
    except ListingUnavailable as exc:
        return not_verifiable(str(exc), base)
    info = find_tool(tools, tool)
    if info is None:
        return not_verifiable(
            f"{tool!r} is not in tools/list, so its {what} cannot be compared "
            "(change.inventory reports the orphan)",
            base,
        )
    if not recorded:
        return not_verifiable(f"the manifest records no {field} for {tool!r}", base)
    live = getattr(info, field)
    evidence = {**base, "recorded": recorded, "live": live}
    if live == recorded:
        return CheckResult("pass", f"{what} unchanged ({field} {live})", evidence)
    return CheckResult(
        "changed",
        f"{what} changed since the last sync: {field} {recorded} -> {live}",
        evidence,
    )


@engine_check("change.schema_drift")
def check_change__schema_drift(
    point: dict, target: McpTarget, *, authenticated: bool
) -> CheckResult:
    """change.schema_drift (CHANGE-2): is the input schema the one recorded?

    Compares the point's ``schema_hash`` with the live listing's (a hash of the
    input schema over canonical JSON, so key order cannot move it). Never calls a
    tool.

    - ``pass`` — the hashes match.
    - ``changed`` — they differ; evidence carries both. The tool now accepts
      something different from what was reviewed. Sync turns this into drift.
    - ``not_verifiable`` — no listing, the tool is not listed (change.inventory
      reports it), or the manifest recorded no hash.

    Standards: OWASP Agentic Top 10 ASI04 (Agentic Supply Chain) — a tool whose
    contract moves under a pinned review; the Semgrep MCP security cheatsheet,
    client tab, rows 12 and 14 (untrusted descriptions, name collisions).
    """
    return _hash_check("schema_hash", "input schema", point, target, authenticated)


@engine_check("change.description_drift")
def check_change__description_drift(
    point: dict, target: McpTarget, *, authenticated: bool
) -> CheckResult:
    """change.description_drift (CHANGE-3): does the tool still say the same?

    Compares the point's ``description_hash`` with the live listing's. A
    description is text a model reads and acts on, so a reworded one is a change
    to the agent's instructions even when the schema is untouched — the shape of
    a tool-poisoning or rug-pull. Never calls a tool.

    - ``pass`` — the hashes match.
    - ``changed`` — they differ; evidence carries both hashes (the manifest keeps
      only the hash, so the previous text is not available to show).
    - ``not_verifiable`` — no listing, the tool is not listed (change.inventory
      reports it), or the manifest recorded no hash.

    Standards: OWASP Agentic Top 10 ASI04 (Agentic Supply Chain); OWASP LLM Top
    10 LLM01 (prompt injection, via the description); the Semgrep MCP security
    cheatsheet, client tab, row 12 (tool descriptions treated as untrusted).
    """
    return _hash_check("description_hash", "description", point, target, authenticated)
