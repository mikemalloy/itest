"""Read a promptfoo results file into a neutral run record.

File I/O and arithmetic, nothing else: promptfoo is never run, no model is
called, and the file is read exactly as promptfoo 0.123 wrote it. Two result
shapes are understood, and the reader records which it found — a results file
never has to say which kind it is:

**Agent target** (a provider that drives an agent over the server's tools):
every tool the agent called is in ``row.metadata.toolCalls[]``, each entry
``{id, name, input, output, is_error}``. Each entry is one call; ``is_error``
says whether the server refused it.

**Direct MCP-provider target** (promptfoo's ``mcp`` provider, one tool per
test): the row carries ``row.metadata.toolName`` — singular — plus ``toolArgs``
and ``originalPayload``. ``toolName`` counts as one call, and ``row.success``
stands in for refused / succeeded (``True`` succeeded, ``False`` refused).
That mapping is the one place the two shapes differ.

Run identity is ``$.evalId``; run time ``$.results.timestamp``; tool version
``$.metadata.promptfooVersion``. Per row the reader reads ``success``,
``testCase.description``, ``provider.id`` and ``provider.label``. Rows that
name no tool contribute to the denominator and to the source-level notes,
never to a tool row.

**Targeting is recorded only when the harness declares it**, as
``row.testCase.metadata.target_tool``. It is never inferred from prompt text:
every prompt in the fixture names ``delete_record``, and none of that is
evidence about targeting. Without a declaration the lane reads "targeting not
declared by the harness".

A tool that was never called (and never declared a target) is **absent** from
``per_tool``. The reader says so by absence, never by inventing a zero row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: The kind this reader serves; the source's ``kind`` value.
KIND = "promptfoo"

#: The result shapes the reader distinguishes.
SHAPES = ("agent", "direct", "mixed", "none")

#: The note recorded when no row declares ``target_tool``.
TARGETING_NOT_DECLARED = "targeting not declared by the harness"


class EvidenceReadError(Exception):
    """A results file that cannot be read as promptfoo output. The message
    names the file and the reason; sync records it as ``unreadable``."""


@dataclass(frozen=True)
class ToolEvidence:
    """What one run says about one tool. Counts, never a judgment."""

    #: Rows whose calls named this tool at least once.
    rows_with_call: int
    #: Call entries naming it, across every row (a row may call it twice).
    calls: int
    #: Entries the server refused (``is_error``; ``success`` false in direct shape).
    refused: int
    succeeded: int
    #: Rows in the run: the denominator.
    rows_total: int
    #: Rows whose harness declared this tool as the target; ``None`` when the
    #: harness declared no targeting anywhere in the run.
    targeted: int | None


@dataclass
class RunEvidence:
    """One results file, read. ``unmatched_tools`` is filled by the join."""

    source_name: str
    kind: str
    run_id: str | None
    run_at: str | None
    tool_version: str | None
    agent: str | None
    rows: int
    shape: str
    per_tool: dict[str, ToolEvidence] = field(default_factory=dict)
    unmatched_tools: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class _Tally:
    rows_with_call: int = 0
    calls: int = 0
    refused: int = 0
    succeeded: int = 0
    targeted: int = 0


def _load_document(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise EvidenceReadError(f"{path} does not exist") from None
    except OSError as exc:
        raise EvidenceReadError(f"{path} could not be read: {exc.strerror}") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvidenceReadError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise EvidenceReadError(
            f"{path} is a JSON {type(document).__name__}, not a promptfoo results "
            "document."
        )
    return document


def _rows(document: dict, path: Path) -> list[dict]:
    results = document.get("results")
    rows = results.get("results") if isinstance(results, dict) else None
    if not isinstance(rows, list):
        raise EvidenceReadError(
            f"{path} has no results.results list: not a promptfoo results "
            "document (promptfoo eval --output writes one)."
        )
    return [row for row in rows if isinstance(row, dict)]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _row_shape(metadata: dict) -> str | None:
    if isinstance(metadata.get("toolCalls"), list):
        return "agent"
    if _string(metadata.get("toolName")):
        return "direct"
    return None


def _run_shape(shapes: set[str]) -> str:
    if not shapes:
        return "none"
    if len(shapes) == 1:
        return next(iter(shapes))
    return "mixed"


def read_results(path: Path, *, source_name: str, agent: str | None) -> RunEvidence:
    """Read ``path`` as promptfoo output. Raises :class:`EvidenceReadError` for
    anything that is not one; never raises anything else for a bad file."""
    document = _load_document(path)
    rows = _rows(document, path)
    results = document.get("results") or {}
    metadata = document.get("metadata") or {}

    tallies: dict[str, _Tally] = {}
    shapes: set[str] = set()
    providers: set[str] = set()
    cases: list[str] = []
    no_tool = 0
    targeting_declared = False

    for row in rows:
        row_metadata = (
            row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        )
        provider = row.get("provider") if isinstance(row.get("provider"), dict) else {}
        label = _string(provider.get("id")) or _string(provider.get("label"))
        if label:
            providers.add(label)
        case = row.get("testCase") if isinstance(row.get("testCase"), dict) else {}
        description = _string(case.get("description"))
        if description and description not in cases:
            cases.append(description)

        case_metadata = (
            case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
        )
        target = _string(case_metadata.get("target_tool"))
        if target:
            targeting_declared = True
            tallies.setdefault(target, _Tally()).targeted += 1

        shape = _row_shape(row_metadata)
        named: set[str] = set()
        if shape == "agent":
            for call in row_metadata["toolCalls"]:
                if not isinstance(call, dict):
                    continue
                name = _string(call.get("name"))
                if not name:
                    continue
                tally = tallies.setdefault(name, _Tally())
                tally.calls += 1
                if call.get("is_error"):
                    tally.refused += 1
                else:
                    tally.succeeded += 1
                named.add(name)
        elif shape == "direct":
            name = row_metadata["toolName"]
            tally = tallies.setdefault(name, _Tally())
            tally.calls += 1
            if row.get("success"):
                tally.succeeded += 1
            else:
                tally.refused += 1
            named.add(name)
        if shape is not None:
            shapes.add(shape)
        if not named:
            no_tool += 1
        for name in named:
            tallies[name].rows_with_call += 1

    notes: list[str] = []
    if no_tool:
        notes.append(f"{no_tool} row(s) named no tool")
    if not targeting_declared:
        notes.append(TARGETING_NOT_DECLARED)
    if providers:
        notes.append(f"providers: {', '.join(sorted(providers))}")
    if cases:
        notes.append(f"cases: {'; '.join(cases)}")

    per_tool = {
        name: ToolEvidence(
            rows_with_call=tally.rows_with_call,
            calls=tally.calls,
            refused=tally.refused,
            succeeded=tally.succeeded,
            rows_total=len(rows),
            targeted=tally.targeted if targeting_declared else None,
        )
        for name, tally in sorted(tallies.items())
    }
    return RunEvidence(
        source_name=source_name,
        kind=KIND,
        run_id=_string(document.get("evalId")),
        run_at=_string(results.get("timestamp")) if isinstance(results, dict) else None,
        tool_version=_string(metadata.get("promptfooVersion"))
        if isinstance(metadata, dict)
        else None,
        agent=agent,
        rows=len(rows),
        shape=_run_shape(shapes),
        per_tool=per_tool,
        notes=notes,
    )
