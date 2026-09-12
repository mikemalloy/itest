"""Family B, Blast radius: what does calling this tool do?

Two engine checks. ``blast.mutation_class`` (BLAST-1) is the **agreement**
check — readonly, from the listing, never calling the tool. Its active-tier
sibling ``blast.mutation_class_observed`` (BLAST-1b) is the **behavioural**
one: snapshot state through the declared snapshot tool, call a read-classified
tool once with sentinel arguments, snapshot again, and compare. It is the
check that catches a tool that lies about itself consistently.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
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
    listing_label,
    not_verifiable,
    reference_listing,
    server_of,
    tool_of,
)
from itest.checks.authority import NoSentinel, sentinel_arguments
from itest.core.declarations.loader import declaration_path
from itest.probes.mcp import (
    MUTATING_CLASSES,
    McpTarget,
    SessionCall,
    ToolInfo,
    classify_mutation,
    session_calls,
)

#: The only classes the observed check will call: a tool that already declares
#: mutation has nothing to catch, and proving it would mean causing it.
OBSERVABLE_CLASSES = frozenset({"read", "informational"})

#: The declaration fact the observed check needs, and the detail without it.
NO_SNAPSHOT_TOOL = (
    "declare observation.snapshot_tool to enable observed mutation-class checking"
)

#: Strictness order for "the stricter of the recorded and the live class".
_RANK = {"informational": 0, "read": 1, "unknown": 2, "write": 3, "destructive": 4}

#: How many changed paths a result names. A snapshot view can hold customer
#: records, tenant data or PII, so a result carries structure only — hashes,
#: sizes, field names or paths — and even the path list is bounded.
CHANGED_PATHS_CAP = 10

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


@engine_check("blast.mutation_class")
def check_blast__mutation_class(
    point: dict, target: McpTarget, *, authenticated: bool
) -> CheckResult:
    """blast.mutation_class (BLAST-1, agreement): do the statements agree?

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
    store — which is the active-tier "mutation class observed" check, not this
    one.

    Standards: OWASP Agentic Top 10 ASI02 (Tool Misuse); OWASP LLM Top 10 LLM03
    (Excessive Agency, 2026 numbering); the OWASP Agent Control Standard's
    AgBOM mutation attribute. Semgrep MCP security cheatsheet: no row mapped —
    it has no row for cross-checking a tool's annotations.
    """
    server, tool = server_of(point), tool_of(point)
    attributes = attributes_of(point)
    recorded = str(attributes.get("mutation") or "unknown")
    provenance = str(attributes.get("mutation_source") or "detected")
    declared = recorded if provenance in _DECLARED else None

    base = {"server": server, "tool": tool, "listing": listing_label(authenticated)}
    try:
        tools = reference_listing(target, authenticated=authenticated)
    except ListingUnavailable as exc:
        return not_verifiable(str(exc), base)
    info = find_tool(tools, tool)
    if info is None:
        return not_verifiable(
            f"{tool!r} is not in tools/list, so its class cannot be read "
            "(change.inventory reports the orphan)",
            base,
        )

    live, live_source = classify_mutation(info)
    hint = _hint(info.annotations)
    name_class = _name_class(info)
    evidence = {
        **base,
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


# --- BLAST-1b: mutation class, observed ---------------------------------------


def _stricter(recorded: str, live: str | None) -> str:
    if live is None:
        return recorded
    return max(recorded, live, key=lambda cls: _RANK.get(cls, _RANK["unknown"]))


def _claimed_by(info: ToolInfo | None, mutation_evidence: str | None) -> str:
    """What claimed the class, for a reader: the annotation, or the name."""
    if info is not None:
        hint = _hint(info.annotations)
        if hint is not None:
            return f"annotation {hint}"
        return f"name {_name_pattern(info.name)}"
    if mutation_evidence and mutation_evidence != "unknown":
        return mutation_evidence
    return "the manifest"


def _view(result: Any) -> Any:
    """A snapshot result as comparable data: the structured content when the
    server sent one, else the text blocks (parsed as JSON where they are)."""
    raw = result.raw or {}
    structured = raw.get("structured_content", raw.get("structuredContent"))
    if structured is not None:
        return structured
    texts = [
        block.get("text", "")
        for block in raw.get("content") or []
        if isinstance(block, dict)
    ]
    parsed = []
    for text in texts:
        try:
            parsed.append(json.loads(text))
        except (TypeError, ValueError):
            parsed.append(text)
    return parsed[0] if len(parsed) == 1 else parsed


def _leaves(view: Any, path: str = "") -> dict[str, Any]:
    """Every scalar in ``view`` by its path (``customers[1].email``)."""
    if isinstance(view, dict):
        out: dict[str, Any] = {}
        for key in sorted(view, key=str):
            out.update(_leaves(view[key], f"{path}.{key}" if path else str(key)))
        return out
    if isinstance(view, list):
        out = {}
        for index, item in enumerate(view):
            out.update(_leaves(item, f"{path}[{index}]"))
        return out
    return {path or "$": view}


def _snapshot(result: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(summary, leaves)``: what persists, and what is compared in memory.

    The summary is structure only — a hash of the canonical view and the
    number of scalar values in it. The view itself never leaves this module:
    it can hold customer records, tenant data or PII, and a result's detail
    flows into verify JSON and the rendered report.
    """
    view = _view(result)
    canonical = json.dumps(view, sort_keys=True, separators=(",", ":"), default=str)
    leaves = _leaves(view)
    summary = {
        "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12],
        "size": len(leaves),
    }
    return summary, leaves


def _changed_paths(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The paths whose value differs, or that exist on one side only. Names and
    positions, never values."""
    return sorted(
        path
        for path in set(before) | set(after)
        if path not in before or path not in after or before[path] != after[path]
    )


def _describe_change(
    snapshot_tool: str, before: dict, after: dict, paths: list[str]
) -> str:
    """One line, structural: sizes, how many paths moved, which (bounded),
    and the two hashes. ``search_records: 3 -> 4 values, 2 path(s) changed
    (ids[0], total) (before 50389d988895, after aba90f872549)``."""
    shown = ", ".join(paths[:CHANGED_PATHS_CAP])
    if len(paths) > CHANGED_PATHS_CAP:
        shown += f", and {len(paths) - CHANGED_PATHS_CAP} more"
    return (
        f"{snapshot_tool!r}: {before['size']} -> {after['size']} values, "
        f"{len(paths)} path(s) changed ({shown}) (before {before['hash']}, "
        f"after {after['hash']})"
    )


@engine_check("blast.mutation_class_observed")
def check_blast__mutation_class_observed(
    point: dict, target: McpTarget, *, authenticated: bool
) -> CheckResult:
    """blast.mutation_class_observed (BLAST-1b, active): does a read tool
    change state?

    The agreement check compares statements, and a tool whose annotation and
    name agree and are both false passes it. This check ignores the statements
    and watches: snapshot observable state, call the tool once with sentinel
    arguments, snapshot again, compare. Three calls in ONE session, so an
    in-memory server keeps its state between them.

    What "observable state" is, is a declared fact, never a guess:
    ``observation.snapshot_tool`` names a read tool whose output is a stable,
    comparable view (for reference-mcp, ``search_records``). It is called with
    the same sentinel arguments, and its result — the structured content the
    server sent, or its text — is normalised and hashed.

    - ``critical`` — the two snapshots differ: the tool mutated while claiming
      not to. The detail names the tool, its claimed class, what claimed it
      (the annotation, or the name) and what changed — structurally: sizes,
      the changed field names or paths (at most :data:`CHANGED_PATHS_CAP`,
      and it says when there are more) and the two hashes. Never a value:
      a snapshot can hold customer data, and a detail flows into verify JSON
      and the report, past the credential scrubber. Stop and escalate: an
      agent has been told this tool is safe to call freely.
    - ``pass`` — no observable change. One call, one observation window: a
      tool that mutates only under other conditions, or in state the snapshot
      tool does not show, is not caught, and the detail says so.
    - ``not_verifiable`` — no ``observation.snapshot_tool`` declared (never a
      pass); the snapshot tool is not a read tool, is not listed, or answered
      with an error; the tool under test is not listed, has no sentinel form,
      or the session was refused or failed; or the tool's class (the stricter
      of the manifest's and the live listing's) is write, destructive or
      unknown — this check NEVER calls a mutating tool, and nothing is opened.

    Active tier: proving a read tool mutates means causing that mutation once,
    so it runs only where the committed policy allows ``active`` and never in
    production. The session path it uses has no mutation opt-in at all.

    Standards: OWASP Agentic Top 10 ASI02 (Tool Misuse); OWASP LLM Top 10
    LLM03 (Excessive Agency). Semgrep MCP security cheatsheet: no row mapped.
    """
    server, tool = server_of(point), tool_of(point)
    attributes = attributes_of(point)
    recorded = str(attributes.get("mutation") or "unknown")
    if "snapshot_tool" in attributes:
        snapshot_tool = attributes.get("snapshot_tool")
    else:
        # A manifest from before the fact was recorded: read the declaration.
        try:
            snapshot_tool = declaration_for(server).observation.snapshot_tool
        except DeclarationMissing:
            snapshot_tool = None
    evidence: dict[str, Any] = {
        "server": server,
        "tool": tool,
        "listing": listing_label(authenticated),
        "snapshot_tool": snapshot_tool,
        "recorded_mutation": recorded,
        "live_mutation": None,
        "claimed": recorded,
        "claimed_by": None,
        "called": False,
        "arguments": None,
    }
    if not snapshot_tool:
        return not_verifiable(NO_SNAPSHOT_TOOL, evidence)

    try:
        tools = reference_listing(target, authenticated=authenticated)
    except ListingUnavailable as exc:
        return not_verifiable(str(exc), evidence)
    info = find_tool(tools, tool)
    live = classify_mutation(info)[0] if info is not None else None
    cls = _stricter(recorded, live)
    evidence.update(
        live_mutation=live,
        claimed=cls,
        claimed_by=_claimed_by(info, attributes.get("mutation_evidence")),
    )
    if cls in MUTATING_CLASSES:
        return not_verifiable(
            f"{tool!r} is {cls}: it already declares that it mutates, so there is "
            "nothing to observe and this check never calls it",
            evidence,
        )
    if cls not in OBSERVABLE_CLASSES:
        return not_verifiable(
            f"the class of {tool!r} is {cls}, so it was not called", evidence
        )
    if info is None:
        return not_verifiable(
            f"{tool!r} is not in tools/list, so it was not called "
            "(change.inventory reports the orphan)",
            evidence,
        )
    snapshot_info = find_tool(tools, snapshot_tool)
    if snapshot_info is None:
        return not_verifiable(
            f"observation.snapshot_tool {snapshot_tool!r} is not in tools/list",
            evidence,
        )
    snapshot_class = classify_mutation(snapshot_info)[0]
    if snapshot_class not in OBSERVABLE_CLASSES:
        return not_verifiable(
            f"observation.snapshot_tool {snapshot_tool!r} is {snapshot_class}; a "
            "snapshot is taken only through a read tool",
            evidence,
        )

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
        snapshot_arguments = sentinel_arguments(snapshot_info.input_schema, sentinel)
    except NoSentinel as exc:
        return not_verifiable(str(exc), evidence)

    evidence.update(called=True, arguments=arguments)
    before, call, after = session_calls(
        target,
        [
            SessionCall(snapshot_tool, snapshot_arguments, snapshot_class),
            SessionCall(tool, arguments, cls),
            SessionCall(snapshot_tool, snapshot_arguments, snapshot_class),
        ],
        authenticated=authenticated,
        base_dir=Path.cwd(),
    )
    evidence["call_status"] = call.status
    if before.status != "ok" or after.status != "ok":
        failed = before if before.status != "ok" else after
        evidence["called"] = before.status == "ok" and call.raw is not None
        return not_verifiable(
            f"could not snapshot through {snapshot_tool!r}: {failed.detail}",
            evidence,
        )
    if call.raw is None:
        evidence["called"] = False
        return not_verifiable(
            f"{tool!r} was not called: {call.detail}",
            evidence,
        )

    # Compared in memory; only structure persists (hashes, sizes, paths).
    evidence["before"], leaves_before = _snapshot(before)
    evidence["after"], leaves_after = _snapshot(after)
    paths = _changed_paths(leaves_before, leaves_after)
    evidence["changed_paths"] = paths[:CHANGED_PATHS_CAP]
    evidence["changed_paths_total"] = len(paths)
    evidence["changed_paths_truncated"] = len(paths) > CHANGED_PATHS_CAP
    # The tool's own answer is not quoted: a server can echo data into it.
    ran = "answered" if call.status == "ok" else "answered with a tool error"
    if evidence["before"]["hash"] == evidence["after"]["hash"]:
        return CheckResult(
            "pass",
            f"no observable change through {snapshot_tool!r} after one call of "
            f"{tool!r} with sentinel arguments (it {ran}); one call, one "
            "observation window",
            evidence,
        )
    changed = _describe_change(
        snapshot_tool, evidence["before"], evidence["after"], paths
    )
    return CheckResult(
        "critical",
        f"{tool!r} claims {cls} ({evidence['claimed_by']}) but changed observable "
        f"state: {changed}. One sentinel call did this (it {ran})",
        evidence,
    )
