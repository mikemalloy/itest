"""The promptfoo reader, against a real results file.

``tests/fixtures/evidence/promptfoo-reference-mcp-2026-09-23.json`` is the
output of a real promptfoo 0.123 run against ``examples/reference-mcp``: four
agent providers (two models, each with a clean and a poisoned tool proxy),
three test cases, twelve rows. It is the contract for the reader: every row,
every metadata field.

The reader is file I/O and arithmetic. It never runs promptfoo, never shells
out, never calls a model, and it records what the file says — a tool that was
never called is absent from ``per_tool``, never invented as a zero row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from itest.core.evidence.promptfoo import (
    EvidenceReadError,
    RunEvidence,
    ToolEvidence,
    read_results,
)

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "evidence"
    / "promptfoo-reference-mcp-2026-09-23.json"
)


@pytest.fixture(scope="module")
def run() -> RunEvidence:
    return read_results(FIXTURE, source_name="promptfoo-lab", agent="sonnet-5")


@pytest.fixture(scope="module")
def document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- run identity ------------------------------------------------------------------


def test_run_identity_is_the_files_own(run: RunEvidence, document: dict) -> None:
    assert run.source_name == "promptfoo-lab"
    assert run.kind == "promptfoo"
    assert run.agent == "sonnet-5"
    assert run.run_id == document["evalId"] == "eval-zyk-2026-09-23T18:26:29"
    assert run.run_at == document["results"]["timestamp"] == "2026-09-23T18:26:29.565Z"
    assert run.tool_version == document["metadata"]["promptfooVersion"] == "0.123.0"


def test_the_fixture_is_an_agent_run_of_twelve_rows(run: RunEvidence) -> None:
    assert run.shape == "agent"
    assert run.rows == 12


# --- per tool -----------------------------------------------------------------------


def test_the_tools_the_agent_called_are_counted(run: RunEvidence) -> None:
    assert set(run.per_tool) == {"get_guide", "search_records"}
    guide = run.per_tool["get_guide"]
    assert guide.rows_total == 12
    assert guide.rows_with_call == 10
    assert guide.calls == 10
    assert guide.refused == 0
    assert guide.succeeded == 10
    search = run.per_tool["search_records"]
    assert search.rows_total == 12
    assert search.rows_with_call == 5
    assert search.calls == 6  # one row called it twice
    assert search.refused == 0
    assert search.succeeded == 6


def test_a_tool_never_called_is_absent_not_a_zero_row(run: RunEvidence) -> None:
    """delete_record is the injection's target and was never called: the
    induced-and-contained case did not occur in this run, and the reader says
    so by absence."""
    assert "delete_record" not in run.per_tool


def test_targeting_is_never_inferred_from_prompt_text(run: RunEvidence) -> None:
    """Every prompt names delete_record; no row's testCase.metadata declares
    a target_tool. Targeted is unknown, not twelve."""
    assert all(t.targeted is None for t in run.per_tool.values())
    assert "targeting not declared by the harness" in run.notes


def test_rows_with_no_tool_call_count_in_the_denominator_only(
    run: RunEvidence,
) -> None:
    assert any("2 row(s) named no tool" == note for note in run.notes)
    assert run.per_tool["get_guide"].rows_total == 12


def test_the_source_line_names_providers_and_cases(run: RunEvidence) -> None:
    assert (
        "providers: haiku-clean, haiku-poisoned, sonnet-clean, sonnet-poisoned"
        in run.notes
    )
    cases = next(note for note in run.notes if note.startswith("cases: "))
    assert "BASELINE" in cases and "INDIRECT INJECTION" in cases


def test_unmatched_tools_are_empty_until_the_join(run: RunEvidence) -> None:
    assert run.unmatched_tools == []


# --- the two shapes ----------------------------------------------------------------


def _write(tmp_path: Path, rows: list[dict], **top: object) -> Path:
    document = {
        "evalId": "eval-direct-1",
        "results": {"timestamp": "2026-09-20T10:00:00.000Z", "results": rows},
        "metadata": {"promptfooVersion": "0.123.0"},
        **top,
    }
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _direct_row(tool: str, success: bool, target: str | None = None) -> dict:
    row = {
        "success": success,
        "provider": {"id": "mcp", "label": "mcp-direct"},
        "testCase": {"description": f"call {tool}", "metadata": {}},
        "metadata": {
            "toolName": tool,
            "toolArgs": {"query": "x"},
            "originalPayload": {"name": tool, "arguments": {"query": "x"}},
        },
    }
    if target is not None:
        row["testCase"]["metadata"]["target_tool"] = target
    return row


def test_a_direct_mcp_provider_run_is_read_as_direct_shape(tmp_path: Path) -> None:
    """promptfoo's ``mcp`` provider: one tool per test, ``metadata.toolName``
    singular. ``toolName`` is one call and ``row.success`` stands in for
    refused / succeeded — the one place the two shapes differ."""
    path = _write(
        tmp_path,
        [
            _direct_row("search_records", True),
            _direct_row("search_records", False),
            _direct_row("delete_record", False),
        ],
    )
    run = read_results(path, source_name="direct", agent=None)
    assert run.shape == "direct"
    assert run.rows == 3
    assert run.run_id == "eval-direct-1"
    assert run.per_tool["search_records"] == ToolEvidence(
        rows_with_call=2,
        calls=2,
        refused=1,
        succeeded=1,
        rows_total=3,
        targeted=None,
    )
    assert run.per_tool["delete_record"].refused == 1


def test_a_row_naming_no_tool_contributes_to_the_denominator_only(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "success": True,
            "provider": {"id": "p", "label": "agent"},
            "testCase": {"description": "refused outright", "metadata": {}},
            "metadata": {"toolCalls": [], "toolNames": []},
        },
        {
            "success": True,
            "provider": {"id": "p", "label": "agent"},
            "testCase": {"description": "read", "metadata": {}},
            "metadata": {
                "toolCalls": [
                    {"id": "c1", "name": "get_guide", "input": {}, "is_error": False}
                ],
                "toolNames": ["get_guide"],
            },
        },
    ]
    run = read_results(_write(tmp_path, rows), source_name="s", agent=None)
    assert run.rows == 2
    assert set(run.per_tool) == {"get_guide"}
    assert run.per_tool["get_guide"].rows_total == 2
    assert run.per_tool["get_guide"].rows_with_call == 1
    assert "1 row(s) named no tool" in run.notes


def test_a_declared_target_is_recorded_only_from_testcase_metadata(
    tmp_path: Path,
) -> None:
    """``testCase.metadata.target_tool`` is the harness saying which tool a row
    tried to induce. Recorded when present — including for a tool the agent
    never called, which is the not-inducible evidence — and never inferred."""
    rows = [
        _direct_row("search_records", True, target="delete_record"),
        _direct_row("search_records", True, target="delete_record"),
        _direct_row("get_guide", True),
    ]
    run = read_results(_write(tmp_path, rows), source_name="s", agent=None)
    assert run.per_tool["delete_record"] == ToolEvidence(
        rows_with_call=0,
        calls=0,
        refused=0,
        succeeded=0,
        rows_total=3,
        targeted=2,
    )
    assert run.per_tool["search_records"].targeted == 0
    assert run.per_tool["get_guide"].targeted == 0
    assert "targeting not declared by the harness" not in run.notes


def test_a_run_mixing_both_shapes_is_mixed_and_one_with_neither_is_none(
    tmp_path: Path,
) -> None:
    agent_row = {
        "success": True,
        "provider": {"id": "p", "label": "agent"},
        "testCase": {"description": "d", "metadata": {}},
        "metadata": {
            "toolCalls": [
                {"id": "c1", "name": "get_guide", "input": {}, "is_error": True}
            ]
        },
    }
    mixed = read_results(
        _write(tmp_path, [agent_row, _direct_row("get_guide", True)]),
        source_name="s",
        agent=None,
    )
    assert mixed.shape == "mixed"
    assert mixed.per_tool["get_guide"].calls == 2
    assert mixed.per_tool["get_guide"].refused == 1
    bare = {
        "success": True,
        "provider": {"id": "p", "label": "agent"},
        "testCase": {"description": "d", "metadata": {}},
        "metadata": {},
    }
    none = read_results(_write(tmp_path, [bare]), source_name="s", agent=None)
    assert none.shape == "none"
    assert none.per_tool == {}
    assert none.rows == 1


# --- what cannot be read -----------------------------------------------------------


def test_a_missing_file_is_a_read_error_not_an_exception_of_another_kind(
    tmp_path: Path,
) -> None:
    with pytest.raises(EvidenceReadError) as excinfo:
        read_results(tmp_path / "nope.json", source_name="s", agent=None)
    assert "nope.json" in str(excinfo.value)


def test_invalid_json_is_a_read_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(EvidenceReadError) as excinfo:
        read_results(path, source_name="s", agent=None)
    assert "not valid JSON" in str(excinfo.value)


def test_a_document_without_promptfoo_results_is_a_read_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(EvidenceReadError) as excinfo:
        read_results(path, source_name="s", agent=None)
    assert "results.results" in str(excinfo.value)


def test_a_file_that_is_not_utf8_is_a_read_error(tmp_path: Path) -> None:
    """A results file redirected through a shell that writes UTF-16 is not a
    crash: the reader never raises anything but EvidenceReadError for a bad
    file, and sync records it as unreadable."""
    path = tmp_path / "utf16.json"
    path.write_bytes(json.dumps({"evalId": "x"}).encode("utf-16"))
    with pytest.raises(EvidenceReadError) as excinfo:
        read_results(path, source_name="s", agent=None)
    assert "not UTF-8" in str(excinfo.value)
