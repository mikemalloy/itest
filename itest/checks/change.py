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
    manifest_tools,
    not_verifiable,
    reference_listing,
    server_of,
    tool_of,
)
from itest.probes.mcp import McpTarget


@engine_check("D1")
def check_d1(point: dict, target: McpTarget, *, authenticated: bool) -> CheckResult:
    """D1 inventory: does the server still list the tool the manifest records?

    Reads one live ``tools/list`` (cached per server per run) and never calls a
    tool.

    - ``pass`` — the tool is in the listing.
    - ``fail`` — it is not: the manifest point is an orphan. A tool that
      vanished may have been withdrawn, renamed, or hidden from this caller.
    - ``not_verifiable`` — the listing could not be taken (unreachable, refused,
      no credential for an authenticated listing).

    Every D1 result for a server carries ``evidence.undeclared``: live tools with
    no manifest point — tools an agent can call that nobody reviewed. It is the
    same list on each result (reported once per server, repeated so any one row
    shows it), ``[]`` when there are none, and ``None`` when there is no manifest
    to compare against.

    Standards: OWASP Agentic Top 10 ASI04 (Agentic Supply Chain); the Semgrep
    MCP security cheatsheet, client tab, row 14 (name collisions — a newly
    appearing, unreviewed tool is where a colliding name comes from).
    """
    server, tool = server_of(point), tool_of(point)
    try:
        tools = reference_listing(target, authenticated=authenticated)
    except ListingUnavailable as exc:
        return not_verifiable(str(exc), {"server": server, "tool": tool})

    recorded = manifest_tools(server)
    undeclared = (
        None if recorded is None else sorted({t.name for t in tools} - set(recorded))
    )
    evidence = {
        "server": server,
        "tool": tool,
        "listed": find_tool(tools, tool) is not None,
        "undeclared": undeclared,
        "listing": "authenticated" if authenticated else "anonymous",
    }
    if evidence["listed"]:
        return CheckResult("pass", f"{tool!r} is listed by {server}", evidence)
    return CheckResult("fail", f"{tool!r} is not in tools/list; orphan", evidence)


def _hash_check(
    field: str, what: str, point: dict, target: McpTarget, authenticated: bool
) -> CheckResult:
    server, tool = server_of(point), tool_of(point)
    recorded = attributes_of(point).get(field)
    try:
        tools = reference_listing(target, authenticated=authenticated)
    except ListingUnavailable as exc:
        return not_verifiable(str(exc), {"server": server, "tool": tool})
    info = find_tool(tools, tool)
    if info is None:
        return not_verifiable(
            f"{tool!r} is not in tools/list, so its {what} cannot be compared "
            "(D1 reports the orphan)",
            {"server": server, "tool": tool},
        )
    if not recorded:
        return not_verifiable(
            f"the manifest records no {field} for {tool!r}",
            {"server": server, "tool": tool},
        )
    live = getattr(info, field)
    evidence = {"server": server, "tool": tool, "recorded": recorded, "live": live}
    if live == recorded:
        return CheckResult("pass", f"{what} unchanged ({field} {live})", evidence)
    return CheckResult(
        "changed",
        f"{what} changed since the last sync: {field} {recorded} -> {live}",
        evidence,
    )


@engine_check("D2")
def check_d2(point: dict, target: McpTarget, *, authenticated: bool) -> CheckResult:
    """D2 schema drift: is the tool's input schema the one the manifest recorded?

    Compares the point's ``schema_hash`` with the live listing's (a hash of the
    input schema over canonical JSON, so key order cannot move it). Never calls a
    tool.

    - ``pass`` — the hashes match.
    - ``changed`` — they differ; evidence carries both. The tool now accepts
      something different from what was reviewed. Sync turns this into drift.
    - ``not_verifiable`` — no listing, the tool is not listed (D1 reports it), or
      the manifest recorded no hash.

    Standards: OWASP Agentic Top 10 ASI04 (Agentic Supply Chain) — a tool whose
    contract moves under a pinned review; the Semgrep MCP security cheatsheet,
    client tab, rows 12 and 14 (untrusted descriptions, name collisions).
    """
    return _hash_check("schema_hash", "input schema", point, target, authenticated)


@engine_check("D3")
def check_d3(point: dict, target: McpTarget, *, authenticated: bool) -> CheckResult:
    """D3 description drift: does the tool still say what it said when reviewed?

    Compares the point's ``description_hash`` with the live listing's. A
    description is text a model reads and acts on, so a reworded one is a change
    to the agent's instructions even when the schema is untouched — the shape of
    a tool-poisoning or rug-pull. Never calls a tool.

    - ``pass`` — the hashes match.
    - ``changed`` — they differ; evidence carries both hashes (the manifest keeps
      only the hash, so the previous text is not available to show).
    - ``not_verifiable`` — no listing, the tool is not listed (D1 reports it), or
      the manifest recorded no hash.

    Standards: OWASP Agentic Top 10 ASI04 (Agentic Supply Chain); OWASP LLM Top
    10 LLM01 (prompt injection, via the description); the Semgrep MCP security
    cheatsheet, client tab, row 12 (tool descriptions treated as untrusted).
    """
    return _hash_check("description_hash", "description", point, target, authenticated)
