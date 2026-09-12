"""``session_calls``: several ``tools/call`` in ONE session.

The observed mutation-class check needs three calls against the same state —
a snapshot, the call under test, a snapshot — and over stdio every
``call_tool`` spawns a fresh server whose in-memory store starts over. So the
probe gains a single-session form of the same operation, under the same rules:
a write or destructive class refuses the whole sequence before anything is
opened, there is no ``allow_mutating`` at all, and every string is scrubbed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from itest.probes import mcp as mcp_probe
from itest.probes.mcp import McpTarget, SessionCall, call_tool, session_calls

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "reference-mcp" / "server.py"
)
_spec = importlib.util.spec_from_file_location("reference_mcp_server", _SERVER_PATH)
assert _spec and _spec.loader
reference_mcp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reference_mcp
_spec.loader.exec_module(reference_mcp)


def _stdio() -> McpTarget:
    return McpTarget(kind="stdio", command=[sys.executable, str(_SERVER_PATH)])


def _views(result: Any) -> int:
    raw = result.raw or {}
    structured = raw.get("structured_content") or raw.get("structuredContent") or {}
    return int(structured.get("views", -1))


def test_calls_in_one_session_see_the_same_state() -> None:
    """Two lookalike_read calls on r-1 in one session: the second sees the
    first's write. Two separate call_tool calls each spawn a fresh server."""
    results = session_calls(
        _stdio(),
        [
            SessionCall("lookalike_read", {"id": "r-1"}, "read"),
            SessionCall("lookalike_read", {"id": "r-1"}, "read"),
        ],
        authenticated=False,
    )
    assert [r.status for r in results] == ["ok", "ok"]
    assert [_views(r) for r in results] == [1, 2]
    separate = [
        call_tool(
            _stdio(),
            "lookalike_read",
            {"id": "r-1"},
            authenticated=False,
            mutation_class="read",
        )
        for _ in range(2)
    ]
    assert [_views(r) for r in separate] == [1, 1]


def test_a_mutating_call_anywhere_refuses_the_whole_sequence_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("a session was opened despite a mutating call")

    monkeypatch.setattr(mcp_probe, "_client", explode)
    results = session_calls(
        _stdio(),
        [
            SessionCall("search_records", {"query": "x"}, "read"),
            SessionCall("delete_record", {"id": "x", "confirm": False}, "destructive"),
        ],
        authenticated=False,
    )
    assert [r.status for r in results] == ["refused", "refused"]
    assert all("delete_record" in r.detail for r in results)
    assert all("session_calls never mutates" in r.detail for r in results)


def test_there_is_no_allow_mutating_on_a_session() -> None:
    import inspect

    assert "allow_mutating" not in inspect.signature(session_calls).parameters


def test_a_tool_error_is_an_outcome_and_the_session_goes_on() -> None:
    results = session_calls(
        _stdio(),
        [
            SessionCall("fetch_record", {"id": "sentinel-cannot-exist-0000"}, "read"),
            SessionCall("get_guide", {}, "informational"),
        ],
        authenticated=False,
    )
    assert results[0].status == "error" and results[0].raw is not None
    assert "404" in results[0].detail
    assert results[1].status == "ok"


def test_a_dead_server_is_one_error_per_call_not_an_exception() -> None:
    dead = McpTarget(kind="stdio", command=[sys.executable, "-c", "pass"])
    results = session_calls(
        dead,
        [SessionCall("get_guide", {}, "informational")] * 2,
        authenticated=False,
    )
    assert [r.status for r in results] == ["error", "error"]
    assert all(r.raw is None for r in results)


def test_the_credential_is_scrubbed_from_a_session_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "zzq-session-token-do-not-log"
    monkeypatch.setenv("REFERENCE_MCP_TOKEN", token)
    target = McpTarget(
        kind="stdio",
        command=[sys.executable, str(_SERVER_PATH)],
        credential_env="REFERENCE_MCP_TOKEN",
    )
    (result,) = session_calls(
        target,
        [SessionCall("fetch_record", {"id": token}, "read")],
        authenticated=True,
    )
    assert token not in result.detail
    assert token not in json.dumps(result.raw)
