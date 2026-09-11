"""Family A, Authority: who may call this tool?

A1 is the one engine check here. The rest of the family (A2 tenant isolation,
A3, A4) needs a second identity or other facts only a person can supply, and
will be generated checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from itest.checks._base import (
    CheckResult,
    DeclarationMissing,
    ListingUnavailable,
    attributes_of,
    declaration_for,
    engine_check,
    find_tool,
    live_listing,
    not_verifiable,
    reference_listing,
    server_of,
    tool_of,
)
from itest.core.declarations.loader import declaration_path
from itest.probes.mcp import (
    CRITICAL,
    MUTATING_CLASSES,
    McpTarget,
    ToolInfo,
    call_tool,
    classify_mutation,
)

#: The only classes A1 will call. Everything else — write, destructive, and
#: ``unknown``, which may be either — is judged from the anonymous session and
#: never called: engine checks are readonly by definition.
CALLABLE_CLASSES = frozenset({"read", "informational"})

#: Strictness order for "the stricter of the recorded and the live class".
_RANK = {"informational": 0, "read": 1, "unknown": 2, "write": 3, "destructive": 4}


class NoSentinel(Exception):
    """A required parameter has no sentinel form. The message names it."""


def sentinel_arguments(schema: dict[str, Any] | None, sentinel: str) -> dict:
    """Arguments that cannot address anything real, built from an input schema.

    Required parameters only — nothing optional is sent. A string gets the
    declaration's ``sentinels.nonexistent_id``; a number or integer ``0``; a
    boolean ``False`` (which is also what refuses a ``confirm``-style gate). Any
    other required type has no value that is safe by construction, and raises
    :class:`NoSentinel` rather than guessing.
    """
    schema = schema or {}
    properties = schema.get("properties") or {}
    arguments: dict[str, Any] = {}
    for name in schema.get("required") or []:
        declared = (properties.get(name) or {}).get("type")
        kinds = declared if isinstance(declared, list) else [declared]
        if "string" in kinds:
            arguments[name] = sentinel
        elif "integer" in kinds or "number" in kinds:
            arguments[name] = 0
        elif "boolean" in kinds:
            arguments[name] = False
        else:
            raise NoSentinel(
                f"required parameter {name!r} is of type {declared!r}, which has "
                "no sentinel form; A1 sends only values that cannot exist"
            )
    return arguments


def _needs_string_sentinel(schema: dict[str, Any] | None) -> bool:
    schema = schema or {}
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        declared = (properties.get(name) or {}).get("type")
        kinds = declared if isinstance(declared, list) else [declared]
        if "string" in kinds:
            return True
    return False


def _stricter(recorded: str, live: str | None) -> str:
    if live is None:
        return recorded
    return max(recorded, live, key=lambda cls: _RANK.get(cls, _RANK["unknown"]))


def _anonymous_session(target: McpTarget, tool: str) -> tuple[dict, ToolInfo | None]:
    """The anonymous ``tools/list``, once per server per run, as evidence."""
    try:
        tools = live_listing(target, anonymous=True)
    except ListingUnavailable as exc:
        status = "refused" if exc.refused else "error"
        return {
            "status": status,
            "detail": str(exc),
            "tool_count": None,
            "lists_tool": None,
        }, None
    info = find_tool(tools, tool)
    return {
        "status": "admitted",
        "detail": f"listed {len(tools)} tools to an unauthenticated caller",
        "tool_count": len(tools),
        "lists_tool": info is not None,
    }, info


@engine_check("A1")
def check_a1(point: dict, target: McpTarget, *, authenticated: bool) -> CheckResult:
    """A1 refuses anonymous: does the server turn away a caller with no credential?

    Always probes **anonymously** — no credential, and over stdio the credential
    variable is stripped from the subprocess environment. ``authenticated`` only
    chooses how the tool's input schema is read when the anonymous listing is
    refused.

    A read or informational tool is **called** once, with sentinel arguments built
    from its input schema (required strings get ``sentinels.nonexistent_id``,
    numbers ``0``, booleans ``false``, nothing optional):

    - ``pass`` — the call was refused ("unauthenticated tools/call refused: ...").
    - ``fail`` — the call was answered: an anonymous caller can read through it.
    - ``not_verifiable`` — the call failed in transport, or reached the tool and
      got a tool error (an in-tool refusal and a sentinel miss look alike), or no
      sentinel could be built.

    A write, destructive or unknown tool is **never called** — the class used is
    the stricter of the manifest's and the live listing's. It is judged from the
    anonymous session instead:

    - ``pass`` — the anonymous session itself was refused, before any tool.
    - ``critical`` — an anonymous session was admitted and lists the write or
      destructive tool: nothing authenticates a caller before it. Being let in is
      the finding; the evidence says ``basis: anonymous-session``.
    - ``not_verifiable`` — the listing failed, the tool is hidden from the
      anonymous listing (hidden is not refused), or its class is unknown.

    The anonymous ``tools/list`` is taken once per server and recorded on every
    result as ``evidence.anonymous_listing``. An anonymous listing is common and
    is not a failure by itself; the report shows it.

    Standards: OWASP Agentic Top 10 ASI03 (Identity and Privilege Abuse); the
    Semgrep MCP security cheatsheet, server tab, row 4.
    """
    server, tool = server_of(point), tool_of(point)
    recorded = str(attributes_of(point).get("mutation") or "unknown")

    session, info = _anonymous_session(target, tool)
    if info is None and session["status"] != "admitted":
        try:
            info = find_tool(
                reference_listing(target, authenticated=authenticated), tool
            )
        except ListingUnavailable:
            info = None
    live = classify_mutation(info)[0] if info is not None else None
    cls = _stricter(recorded, live)

    evidence: dict[str, Any] = {
        "server": server,
        "tool": tool,
        "mutation": cls,
        "recorded_mutation": recorded,
        "live_mutation": live,
        "called": False,
        "arguments": None,
        "basis": "anonymous-session",
        "anonymous_listing": session,
    }

    if cls not in CALLABLE_CLASSES:
        return _judge_session(tool, cls, session, evidence)

    if info is None and session["status"] == "error":
        return not_verifiable(session["detail"], evidence)

    schema = info.input_schema if info is not None else {}
    sentinel = ""
    if _needs_string_sentinel(schema):
        try:
            sentinel = declaration_for(server).sentinels.nonexistent_id
        except DeclarationMissing as exc:
            return not_verifiable(
                f"no sentinels.nonexistent_id to call {tool!r} with: {exc} "
                f"(expected {declaration_path(server)})",
                evidence,
            )
    try:
        arguments = sentinel_arguments(schema, sentinel)
    except NoSentinel as exc:
        return not_verifiable(str(exc), evidence)

    evidence.update(called=True, arguments=arguments, basis="call")
    result = call_tool(
        target,
        tool,
        arguments,
        authenticated=False,
        mutation_class=cls,
        base_dir=Path.cwd(),
    )
    evidence["call_status"] = result.status

    if result.status == "refused":
        return CheckResult(
            "pass", f"unauthenticated tools/call refused: {result.detail}", evidence
        )
    if result.status == "ok":
        return CheckResult(
            "fail",
            f"an unauthenticated tools/call of the {cls} tool {tool!r} was answered: "
            "an anonymous caller can use it",
            evidence,
        )
    if result.status == CRITICAL:
        return CheckResult(CRITICAL, result.detail, evidence)
    if result.raw is not None:
        return not_verifiable(
            f"the unauthenticated call reached {tool!r} and got a tool error "
            f"({result.detail}); an in-tool refusal and a sentinel miss look alike",
            evidence,
        )
    return not_verifiable(f"tools/call failed: {result.detail}", evidence)


def _judge_session(tool: str, cls: str, session: dict, evidence: dict) -> CheckResult:
    """A1 for a tool it will not call: what the anonymous session showed."""
    if session["status"] == "refused":
        return CheckResult(
            "pass",
            f"unauthenticated session refused ({session['detail']}); the {cls} tool "
            f"{tool!r} was not called — engine checks never call a write, "
            "destructive or unknown tool",
            evidence,
        )
    if session["status"] == "error":
        return not_verifiable(
            f"the anonymous listing failed: {session['detail']}", evidence
        )
    if not session["lists_tool"]:
        return not_verifiable(
            f"an anonymous session was admitted but does not list {tool!r}; hidden "
            "is not refused, and it was not called",
            evidence,
        )
    if cls in MUTATING_CLASSES:
        return CheckResult(
            CRITICAL,
            f"CRITICAL: an unauthenticated session was admitted and lists the {cls} "
            f"tool {tool!r}; nothing authenticates a caller before it. It was not "
            "called (engine checks never call a mutating tool), so being let in "
            "is the finding",
            evidence,
        )
    return not_verifiable(
        f"the class of {tool!r} is unknown, so it was not called; an anonymous "
        "session was admitted and lists it",
        evidence,
    )
