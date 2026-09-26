"""The findings of a verify run, in one place for the terminal and the page.

A finding is a check that failed: a critical or failing tool check, or a
failing or errored integration point. `itest verify` prints one entry per
finding and the readiness page's Answer lists the same ones, and both get
the entry — status, source -> target, the trait's title (never its slug),
and one plain sentence — from :func:`findings_for`, so the two can never
disagree about what a finding says.

The sentence is the check's recorded detail made plain: the state hashes,
the "One sentinel call did this" clause and a quoted tool error are dropped
(:func:`plain_detail`), because they are the detail layer's and the manifest
keeps the raw detail for it. A pytest failure or error becomes the one line
that matters (:func:`failure_message`): the assertion's message, or the
exception, never a traceback and never a test node id.

Read from verify's own document alone — its points, tests and tool ledger —
so no manifest is needed: a point that is a declared tool is the ledger's,
and its failure is the check's finding, never listed twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Verdict order: what blocks a release first.
SEVERITY_ORDER = {"critical": 0, "failing": 1, "error": 2}

#: Sentence-cleaning rules, applied in order. Each drops a fragment the
#: detail layer needs and the sentence does not: the two state hashes, the
#: sentinel clause, and the server's own error text quoted after "tool error".
_HASHES = re.compile(r"\s*\(before [0-9a-f]{12}, after [0-9a-f]{12}\)")
_SENTINEL_CLAUSE = re.compile(r"\.?\s*One sentinel call did this(?: \([^)]*\))?")
_QUOTED_TOOL_ERROR = re.compile(r"tool error \((?:[^()]|\([^()]*\))*\)")
#: The MCP annotation's own name is the detail layer's; the sentence says
#: what it means.
_READ_ONLY_HINT = re.compile(r"annotation readOnlyHint")

#: What a pytest failure reads as when no message can be found in it.
NO_MESSAGE = "the test failed"


@dataclass(frozen=True)
class Finding:
    """One failed check, as both the terminal and the page state it."""

    #: ``critical``, ``failing`` or ``error``.
    severity: str
    source: str
    target: str
    #: The trait's title for a tool check; the point's tag for a point.
    check: str
    #: The plain sentence. Empty when nothing was recorded.
    detail: str

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, len(SEVERITY_ORDER))


def plain_detail(detail: str) -> str:
    """The recorded detail as a plain sentence: no state hash, no sentinel
    clause, no quoted tool error, and the read-only annotation named for
    what it means rather than by its spec name. Ends with a period when it
    ended with one before the clause was dropped."""
    text = _HASHES.sub("", detail or "")
    ended = text.rstrip().endswith(")") and "One sentinel call did this" in text
    text = _SENTINEL_CLAUSE.sub("", text)
    text = _QUOTED_TOOL_ERROR.sub("tool error", text)
    text = _READ_ONLY_HINT.sub("read-only annotation", text)
    text = re.sub(r"\s+", " ", text).strip()
    if ended and text and not text.endswith("."):
        text += "."
    return text


def failure_message(detail: str) -> str:
    """The one line that matters in pytest's failure text: the assertion's
    message (without the ``AssertionError:`` prefix), or the exception line
    of an error. A bare ``assert`` carries no message, and a traceback with
    none reads :data:`NO_MESSAGE`."""
    lines = [
        line[1:].strip()
        for line in (detail or "").splitlines()
        if line.startswith("E ") or line == "E"
    ]
    candidates = [
        line
        for line in lines
        if line and not line.startswith("assert ") and line not in ("+", "-")
    ]
    if not candidates:
        return NO_MESSAGE
    message = candidates[0]
    if message.startswith("AssertionError: "):
        message = message[len("AssertionError: ") :]
    return message or NO_MESSAGE


def trait_names() -> dict[str, str]:
    """Trait slug -> the table's human name (``destructive gating``)."""
    from itest.core.declarations.traits import load_traits

    return {trait.id: trait.name for trait in load_traits().traits}


def findings_for(verify: dict, names: dict[str, str]) -> list[Finding]:
    """Every finding in a verify document, critical first, then failing, then
    errored; within a severity, in the document's own order.

    ``verify`` is the ``verify --output json`` document (or its dict form):
    ``tools`` gives the tool checks and which points are declared tools,
    ``points`` the integration points and ``tests`` the pytest text a failing
    or errored point's message is read from.
    """
    found: list[Finding] = []
    tool_points: set[str] = set()
    tools = verify.get("tools") or {}
    for server in tools.get("servers") or []:
        for tool in server.get("tools") or []:
            tool_points.add(str(tool.get("point_id")))
            for check in tool.get("checks") or []:
                if check.get("state") in ("not_applicable", "orphan"):
                    continue
                status = check.get("status")
                if status not in ("critical", "fail"):
                    continue
                trait = str(check.get("trait") or "")
                found.append(
                    Finding(
                        severity="critical" if status == "critical" else "failing",
                        source=str(server.get("server") or ""),
                        target=str(tool.get("name") or ""),
                        check=names.get(trait, check.get("code") or trait),
                        detail=plain_detail(str(check.get("detail") or "")),
                    )
                )
    by_point: dict[str, list[dict]] = {}
    for test in verify.get("tests") or []:
        if test.get("point_id"):
            by_point.setdefault(str(test["point_id"]), []).append(test)
    for point in verify.get("points") or []:
        status = point.get("status")
        if status not in ("failing", "error") or point.get("id") in tool_points:
            continue
        outcome = "failed" if status == "failing" else "error"
        texts = [
            failure_message(str(t.get("detail") or ""))
            for t in by_point.get(str(point.get("id")), [])
            if t.get("outcome") == outcome
        ]
        found.append(
            Finding(
                severity=status,
                source=str(point.get("source") or ""),
                target=str(point.get("target") or ""),
                check=str(point.get("tag") or "integration"),
                detail=texts[0] if texts else "",
            )
        )
    found.sort(key=lambda f: f.rank)
    return found
