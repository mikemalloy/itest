"""Shape harness for the reference MCP server.

``examples/reference-mcp/`` is to the MCP probe what ``examples/reference-api/``
is to the HTTP probe: a server that is allowed to be broken on purpose, so the
branches a real MCP deployment must never show can be exercised locally. This
file pins the shape the probe's tests (``tests/test_mcp_probe.py``) rely on —
eight tools, their annotations, the guarded and open mounts, and the two
deliberate defects.

The two defects are the point, not mistakes:

- **the open mount** (``/open/mcp``) serves the same tools with no bearer check,
  which is how an unauthenticated ``tools/call`` on a *mutating* tool can be
  shown reaching the server at all — the MCP analogue of the reference API's
  ``/leaky/action``;
- **``lookalike_read``** declares ``readOnlyHint=true`` and mutates anyway. Its
  conflict is BEHAVIORAL — the declaration and the name agree with each other
  and both lie about what the code does. No static reading of the tool list can
  catch it; only calling it and watching the store can. That catch is P31's
  work, and this file proves the bait is real by asserting the store changed.

The server runs on a loopback ephemeral port under uvicorn in a daemon thread,
exactly as ``tests/test_reference_api.py`` stands its app up. Nothing here
reaches the network beyond 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

# --- load the reference server by path (its directory name is not importable) ---

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "reference-mcp" / "server.py"
)
_spec = importlib.util.spec_from_file_location("reference_mcp_server", _SERVER_PATH)
assert _spec and _spec.loader
reference_mcp = importlib.util.module_from_spec(_spec)
# Registered before exec: the module defines dataclasses, and @dataclass looks
# its defining module up in sys.modules to resolve annotations.
sys.modules[_spec.name] = reference_mcp
_spec.loader.exec_module(reference_mcp)


#: Every tool the reference server declares. The probe's own tests assert this
#: count too; a tool added here without updating both is a failing test, which
#: is the intended friction.
EXPECTED_TOOLS = {
    "get_guide",
    "search_records",
    "fetch_record",
    "create_record",
    "update_record",
    "delete_record",
    "lookalike_read",
    "enrich",
}


@pytest.fixture(scope="session")
def reference() -> Iterator[Any]:
    """The reference server on a loopback port, plus its live record store.

    Session-scoped: every test below either reads, or writes under an id of its
    own, so no test can be made green or red by another's leftovers.
    """
    with reference_mcp.serve_in_thread() as running:
        yield running


def _authed_client(url: str, token: str) -> Client:
    """A client carrying the bearer token the guarded mount requires."""
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    return Client(streamable_http_client(url, http_client=http))


def _anon_client(url: str) -> Client:
    """A client carrying no credential at all."""
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient()))


# --- the tool list ------------------------------------------------------------


def test_tools_list_returns_eight_tools(reference: Any) -> None:
    async def run() -> set[str]:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    assert anyio.run(run) == EXPECTED_TOOLS


def test_annotations_survive_the_wire(reference: Any) -> None:
    """The hints the server declares are what the client sees. `classify_mutation`
    reads exactly these, so a transport that dropped them would silently demote
    every tool to the name heuristic."""

    async def run() -> dict[str, Any]:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return {t.name: t.annotations for t in (await client.list_tools()).tools}

    annotations = anyio.run(run)

    assert annotations["get_guide"] is None  # informational: no annotations at all
    assert annotations["search_records"].read_only_hint is True
    assert annotations["fetch_record"].read_only_hint is True
    assert annotations["create_record"].read_only_hint is False
    assert annotations["create_record"].destructive_hint is False
    assert annotations["update_record"].read_only_hint is False
    assert annotations["delete_record"].destructive_hint is True
    assert annotations["enrich"].read_only_hint is True
    assert annotations["enrich"].open_world_hint is True
    # The bait: declared read-only, mutates anyway (see the module docstring).
    assert annotations["lookalike_read"].read_only_hint is True


def test_every_tool_has_a_description_and_an_input_schema(reference: Any) -> None:
    """The probe hashes both. A tool with neither would hash a constant and the
    drift check would be asserting nothing."""

    async def run() -> list[Any]:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return list((await client.list_tools()).tools)

    for tool in anyio.run(run):
        assert tool.description, f"{tool.name} has no description"
        assert tool.input_schema.get("type") == "object", tool.name


# --- the guarded mount --------------------------------------------------------


def test_authenticated_fetch_record_works(reference: Any) -> None:
    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool("fetch_record", {"id": "r-1"})

    result = anyio.run(run)
    assert result.is_error is False
    assert "widget" in result.content[0].text


def test_unauthenticated_tools_list_on_the_guarded_mount_is_refused(
    reference: Any,
) -> None:
    """No token, no listing — and the refusal is an HTTP 401, which is what the
    probe classifies as "refused" rather than "error"."""
    seen: list[int] = []

    async def record(response: httpx2.Response) -> None:
        seen.append(response.status_code)

    async def run() -> None:
        http = httpx2.AsyncClient(event_hooks={"response": [record]})
        async with (
            http,
            Client(
                streamable_http_client(reference.guarded_url, http_client=http)
            ) as client,
        ):
            await client.list_tools()

    with pytest.raises(BaseException):  # noqa: B017 - the SDK wraps failures in an exception group
        anyio.run(run)
    assert seen, "no HTTP response was observed at all"
    assert seen[0] in (401, 403), f"guarded mount answered {seen[0]}, not 401/403"


# --- the open mount: the intended red ----------------------------------------


def test_unauthenticated_tools_list_on_the_open_mount_succeeds(reference: Any) -> None:
    """THE INTENDED RED. The open mount serves the full tool list to anyone. A
    run in which this failed would mean the bait was gone."""

    async def run() -> set[str]:
        async with _anon_client(reference.open_url) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    assert anyio.run(run) == EXPECTED_TOOLS


def test_unauthenticated_call_of_a_destructive_tool_is_admitted(
    reference: Any,
) -> None:
    """THE CRITICAL SHAPE. An anonymous caller reaches `delete_record` and gets a
    non-error result back. The id is a sentinel that cannot exist, so nothing is
    destroyed to prove it — the finding is that the call was admitted."""
    sentinel = "sentinel-cannot-exist-0000"

    async def run() -> Any:
        async with _anon_client(reference.open_url) as client:
            return await client.call_tool(
                "delete_record", {"id": sentinel, "confirm": True}
            )

    result = anyio.run(run)
    assert result.is_error is False  # a successful result on a destructive tool
    assert sentinel not in reference.records


# --- the behavioral annotation conflict ---------------------------------------


def test_lookalike_read_mutates_state_despite_read_only_hint(reference: Any) -> None:
    """Declared read-only, mutates anyway — asserted against the live store, in
    the same process the server is running in, so this is the mutation itself
    and not a report of one.

    Do not "fix" the tool. The conflict is deliberate and BEHAVIORAL: the
    annotation and the name both say read, and only calling it reveals the
    write. Catching it is P31's B1 recipe, not this file's job."""
    before = reference.records["r-1"]["views"]

    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool("lookalike_read", {"id": "r-1"})

    result = anyio.run(run)
    assert result.is_error is False
    assert reference.records["r-1"]["views"] == before + 1


def test_lookalike_read_logs_every_call_even_a_miss(reference: Any) -> None:
    """A call with a sentinel id (one that cannot exist) is a 404-shaped tool
    error — and it still writes: every lookup is logged as a new record. That
    is what lets the observed mutation-class check catch the tool with sentinel
    arguments alone, never touching a record that exists."""
    before = len(reference.records)

    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool(
                "lookalike_read", {"id": "sentinel-cannot-exist-0000"}
            )

    result = anyio.run(run)
    assert result.is_error is True
    assert "404" in result.content[0].text
    assert len(reference.records) == before + 1
    logged = [r for r in reference.records.values() if r["id"].startswith("lookup-")]
    assert logged and logged[-1]["name"] == "lookup:sentinel-cannot-exist-0000"


def test_search_records_reports_the_store_size(reference: Any) -> None:
    """`total` is the stable, comparable view the declaration names as its
    snapshot tool: a query that matches nothing still says how big the store
    is, so a record added by any call is visible through it."""

    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool("search_records", {"query": "zz-nothing"})

    result = anyio.run(run)
    assert result.is_error is False
    payload = result.structured_content
    assert payload["ids"] == []
    assert payload["total"] == len(reference.records)


# --- error shapes -------------------------------------------------------------


def test_fetch_record_unknown_id_is_a_404_shaped_error(reference: Any) -> None:
    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool("fetch_record", {"id": "no-such-record"})

    result = anyio.run(run)
    assert result.is_error is True
    assert "404" in result.content[0].text


def test_delete_record_unknown_id_returns_an_error_and_never_raises(
    reference: Any,
) -> None:
    """`delete_record` reports a miss in its payload rather than raising, so a
    sentinel-argument call is a *successful* result. That is what lets the
    unauthenticated-mutating-call catch fire without destroying anything."""

    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool(
                "delete_record", {"id": "no-such-record", "confirm": True}
            )

    result = anyio.run(run)
    assert result.is_error is False
    assert "not found" in result.content[0].text


def test_delete_record_requires_confirmation(reference: Any) -> None:
    """`confirm: false` refuses, and the record survives. Proof the destructive
    tool is not a one-argument trigger."""
    reference.records["r-confirm"] = {"id": "r-confirm", "name": "keep", "views": 0}

    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool(
                "delete_record", {"id": "r-confirm", "confirm": False}
            )

    result = anyio.run(run)
    assert "confirmation" in result.content[0].text
    assert "r-confirm" in reference.records


def test_delete_record_with_confirmation_actually_deletes(reference: Any) -> None:
    reference.records["r-doomed"] = {"id": "r-doomed", "name": "gone", "views": 0}

    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool(
                "delete_record", {"id": "r-doomed", "confirm": True}
            )

    anyio.run(run)
    assert "r-doomed" not in reference.records


def test_search_records_finds_by_substring(reference: Any) -> None:
    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool("search_records", {"query": "widget"})

    result = anyio.run(run)
    assert result.is_error is False
    assert "r-1" in result.content[0].text


def test_create_record_adds_to_the_store(reference: Any) -> None:
    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool("create_record", {"name": "created-by-test"})

    result = anyio.run(run)
    assert result.is_error is False
    assert any(r["name"] == "created-by-test" for r in reference.records.values())


def test_update_record_unknown_id_is_an_error(reference: Any) -> None:
    async def run() -> Any:
        async with _authed_client(reference.guarded_url, reference.token) as client:
            return await client.call_tool(
                "update_record", {"id": "no-such-record", "name": "x"}
            )

    result = anyio.run(run)
    assert result.is_error is True


# --- enrich is egress-SHAPED, and only shaped --------------------------------


def test_enrich_describes_egress_but_makes_no_network_call(
    monkeypatch: Any,
) -> None:
    """`enrich`'s description advertises an external provider; its code calls
    nothing. Proven by blocking every way out of the process — name resolution
    and outbound connect — and calling it against a fresh in-process server, so
    no HTTP transport is standing that could need those itself.

    Blocking `connect`/`getaddrinfo` rather than socket construction is
    deliberate: the event loop builds a socketpair for its own wakeup pipe, and
    a test that tripped on that would be asserting "no sockets exist", not "no
    call leaves the machine".

    The tool exists so a probe can be shown to reason about a *declared* egress
    edge without a reference server that actually egresses."""
    fresh = reference_mcp.build_server()

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("enrich reached the network; it must not egress")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket.socket, "connect_ex", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)

    async def run() -> Any:
        return await fresh.server.call_tool("enrich", {"email": "a@example.test"})

    result = anyio.run(run)
    assert "example.test" in str(result)

    async def described() -> str:
        tools = await fresh.server.list_tools()
        return next(t.description or "" for t in tools if t.name == "enrich")

    assert "external" in anyio.run(described).lower()


# --- the stdio transport ------------------------------------------------------


def test_stdio_transport_serves_the_same_eight_tools() -> None:
    """The same server over stdio — a subprocess, no port, no bearer token.
    Both transports must be provable, because a probe target may be either."""

    async def run() -> set[str]:
        params = StdioServerParameters(command=sys.executable, args=[str(_SERVER_PATH)])
        async with Client(params, read_timeout_seconds=20.0) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    assert anyio.run(run) == EXPECTED_TOOLS


def test_stdio_transport_can_call_a_tool() -> None:
    async def run() -> Any:
        params = StdioServerParameters(command=sys.executable, args=[str(_SERVER_PATH)])
        async with Client(params, read_timeout_seconds=20.0) as client:
            return await client.call_tool("fetch_record", {"id": "r-1"})

    result = anyio.run(run)
    assert result.is_error is False
    assert "widget" in result.content[0].text


# --- the token comes from the environment ------------------------------------


def test_token_defaults_when_the_env_var_is_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv(reference_mcp.TOKEN_ENV, raising=False)
    assert reference_mcp.resolve_token() == reference_mcp.DEFAULT_TOKEN


def test_token_is_read_from_the_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv(reference_mcp.TOKEN_ENV, "a-different-token")
    assert reference_mcp.resolve_token() == "a-different-token"
