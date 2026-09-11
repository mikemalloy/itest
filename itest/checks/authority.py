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
    server_of,
    tool_of,
)
from itest.core.declarations.loader import declaration_path
from itest.probes.mcp import (
    MUTATING_CLASSES,
    McpTarget,
    call_tool,
    classify_mutation,
)

#: The only classes A1 will call. Everything else — write, destructive, and
#: ``unknown``, which may be either — is never called: engine checks are
#: readonly by definition.
CALLABLE_CLASSES = frozenset({"read", "informational"})

#: The front door held: the whole server passes, and no tool is called.
FRONT_DOOR_REFUSED = "server refuses anonymous sessions; per-tool call not attempted"

#: A mutating tool behind an open front door: what only the active tier can prove.
MUTATING_DEFERRED = (
    "anonymous session admitted; this tool mutates, so its own guard can only be "
    "proven by an active-tier call on a non-production environment"
)

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


@engine_check("A1")
def check_a1(point: dict, target: McpTarget, *, authenticated: bool) -> CheckResult:
    """A1 refuses anonymous: does the server turn away a caller with no credential?

    A1 judges only what it observed. It always probes **anonymously** — no
    credential, and over stdio the credential variable is stripped from the
    subprocess environment. ``authenticated`` does not change what it does.

    **1. The front door, once per server.** An anonymous ``initialize`` +
    ``tools/list``, cached for the run.

    - ``pass`` — the server refuses anonymous sessions. Every tool on it passes
      with "server refuses anonymous sessions; per-tool call not attempted", and
      the refusal is in the evidence. This is the common good case.
    - ``not_verifiable`` — the session could not be attempted (unreachable, a
      transport error); the detail says why.

    **2. Behind an open front door, per tool.** The class used is the stricter of
    the manifest's and the live listing's, so a stale manifest cannot talk A1
    into calling a mutating tool.

    - A **read or informational** tool gets one anonymous ``tools/call`` with
      sentinel arguments (required strings get ``sentinels.nonexistent_id``,
      numbers ``0``, booleans ``false``, nothing optional):
      ``pass`` if the call is refused; ``fail`` if the tool answered — "anonymous
      call succeeded on a read tool", data exposure rather than mutation (a tool
      error still means the tool ran for an anonymous caller, and says so);
      ``not_verifiable`` for a transport error or a missing sentinel.
    - A **write or destructive** tool is **not called**: ``not_verifiable``,
      "anonymous session admitted; this tool mutates, so its own guard can only
      be proven by an active-tier call on a non-production environment".
    - An **unknown** tool, or one hidden from the anonymous listing, is not
      called: ``not_verifiable``.

    A1 never returns ``critical``: that status means a *demonstrated* anonymous
    admission on a mutating tool, which only the active-tier A1 can show.

    Standards: OWASP Agentic Top 10 ASI03 (Identity and Privilege Abuse); the
    Semgrep MCP security cheatsheet, server tab, row 4.
    """
    server, tool = server_of(point), tool_of(point)
    recorded = str(attributes_of(point).get("mutation") or "unknown")
    evidence: dict[str, Any] = {
        "server": server,
        "tool": tool,
        "recorded_mutation": recorded,
        "live_mutation": None,
        "mutation": recorded,
        "anonymous_listing": None,
        "anonymous_listing_detail": None,
        "anonymous_tool_count": None,
        "called": False,
        "arguments": None,
    }

    # 1. The front door.
    try:
        tools = live_listing(target, anonymous=True)
    except ListingUnavailable as exc:
        evidence["anonymous_listing_detail"] = str(exc)
        if exc.refused:
            evidence["anonymous_listing"] = "refused"
            return CheckResult("pass", FRONT_DOOR_REFUSED, evidence)
        evidence["anonymous_listing"] = "error"
        return not_verifiable(
            f"the anonymous session could not be attempted: {exc}", evidence
        )
    evidence.update(
        anonymous_listing="admitted",
        anonymous_listing_detail=(
            f"listed {len(tools)} tools to an unauthenticated caller"
        ),
        anonymous_tool_count=len(tools),
    )

    # 2. Behind an open front door.
    info = find_tool(tools, tool)
    live = classify_mutation(info)[0] if info is not None else None
    cls = _stricter(recorded, live)
    evidence.update(live_mutation=live, mutation=cls)

    if cls in MUTATING_CLASSES:
        return not_verifiable(MUTATING_DEFERRED, evidence)
    if cls not in CALLABLE_CLASSES:
        return not_verifiable(
            f"anonymous session admitted; the class of {tool!r} is {cls}, so it "
            "was not called",
            evidence,
        )
    if info is None:
        return not_verifiable(
            f"anonymous session admitted, but {tool!r} is not in the anonymous "
            "listing; it was not called (D1 reports a tool the server no longer "
            "lists)",
            evidence,
        )

    sentinel = ""
    if _needs_string_sentinel(info.input_schema):
        try:
            sentinel = declaration_for(server).sentinels.nonexistent_id
        except DeclarationMissing as exc:
            return not_verifiable(
                f"no sentinels.nonexistent_id to call {tool!r} with: {exc} "
                f"(expected {declaration_path(server)})",
                evidence,
            )
    try:
        arguments = sentinel_arguments(info.input_schema, sentinel)
    except NoSentinel as exc:
        return not_verifiable(str(exc), evidence)

    evidence.update(called=True, arguments=arguments)
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
            f"anonymous call succeeded on a read tool ({tool!r}): data exposure, "
            "not mutation",
            evidence,
        )
    if result.status == "error" and result.raw is not None:
        return CheckResult(
            "fail",
            f"anonymous call reached the read tool {tool!r} and it answered with a "
            f"tool error ({result.detail}): the tool ran for an anonymous caller",
            evidence,
        )
    return not_verifiable(f"tools/call failed: {result.detail}", evidence)
