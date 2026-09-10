"""The MCP probe's http transport must refuse a private host and a foreign scheme.

P29 shipped the transport with no SSRF guard: ``McpTarget(kind="http", url=...)``
would connect to whatever URL it was handed, including ``http://169.254.169.254/``
(cloud metadata) or a loopback port — and a declaration resolves that URL from an
environment variable, which is configuration, not a constant. The HTTP probe has
refused exactly those hosts since F4, so the same refusal is applied here by
*importing its check*, not by copying its list: one host list, one place to fix.

The refusal happens before the transport is built, so a refused target is never
connected to — proven twice below, once against a live server that *would* have
answered and once by making the transport builder explode if it is reached.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from itest.probes import mcp as mcp_probe
from itest.probes.mcp import McpProbeError, McpTarget, call_tool, list_tools

# --- load the reference server by path (its directory name is not importable) ---

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "reference-mcp" / "server.py"
)
_spec = importlib.util.spec_from_file_location("reference_mcp_server", _SERVER_PATH)
assert _spec and _spec.loader
reference_mcp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reference_mcp
_spec.loader.exec_module(reference_mcp)

#: The reference server's eight tools. Pinned in tests/test_reference_mcp.py;
#: repeated here only so this file stands alone.
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


@pytest.fixture(scope="module")
def reference() -> Iterator[Any]:
    """The reference MCP server on a loopback port — a private host by design."""
    with reference_mcp.serve_in_thread(token="guard-test-token") as running:
        yield running


# --- a private host is refused unless the target opts in ----------------------


def test_loopback_is_refused_without_the_opt_in(reference: Any) -> None:
    """The open mount answers anyone, so a refusal here can only be the guard's:
    if the probe had connected, it would have returned the eight tools."""
    target = McpTarget(kind="http", url=reference.open_url)
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(target)
    message = str(excinfo.value)
    assert "refuses" in message
    assert "allow_private_hosts" in message


def test_the_opt_in_lets_a_deliberate_local_target_through(reference: Any) -> None:
    target = McpTarget(kind="http", url=reference.open_url, allow_private_hosts=True)
    assert {tool.name for tool in list_tools(target)} == EXPECTED_TOOLS


def test_call_tool_refuses_a_private_host_too(reference: Any) -> None:
    """`call_tool` reports a server's answer as a status, but a target it cannot
    use at all is a usage error — the same class as an http target with no url."""
    target = McpTarget(kind="http", url=reference.open_url)
    with pytest.raises(McpProbeError):
        call_tool(target, "get_guide", {}, authenticated=False, mutation_class="read")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:8931/mcp",  # loopback literal
        "http://[::1]:8931/mcp",  # IPv6 loopback
        "http://localhost:8931/mcp",  # loopback by name
    ],
)
def test_private_hosts_are_refused(url: str) -> None:
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(McpTarget(kind="http", url=url))
    assert "refuses" in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://ftp.example.com/mcp",
        "ws://example.test/mcp",
    ],
)
def test_a_foreign_scheme_is_refused_even_with_the_opt_in(url: str) -> None:
    """``allow_private_hosts`` loosens the host rule and nothing else: a
    ``file://`` read is not a probe, whatever the host."""
    for allow in (False, True):
        with pytest.raises(McpProbeError) as excinfo:
            list_tools(McpTarget(kind="http", url=url, allow_private_hosts=allow))
        assert "refuses" in str(excinfo.value)


def test_a_refused_target_is_never_connected_to(monkeypatch) -> None:
    """The guard runs before the transport is built, so nothing is opened."""

    def explode(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("the probe built a transport despite the guard")

    monkeypatch.setattr(mcp_probe, "streamable_http_client", explode)
    monkeypatch.setattr(mcp_probe.httpx2, "AsyncClient", explode)

    with pytest.raises(McpProbeError):
        list_tools(McpTarget(kind="http", url="http://169.254.169.254/mcp"))
    with pytest.raises(McpProbeError):
        list_tools(McpTarget(kind="http", url="file:///etc/passwd"))


# --- what the guard must NOT do ----------------------------------------------


def test_a_public_host_passes_the_guard() -> None:
    """A normal DNS name is not a private host. It fails to connect (nothing is
    listening on a .invalid name), and that is an ordinary transport failure —
    not the refusal, which would mean the guard had swallowed a real target."""
    target = McpTarget(kind="http", url="http://mcp.invalid/mcp", timeout_s=5.0)
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(target)
    assert "refuses" not in str(excinfo.value)


def test_a_stdio_target_is_unaffected() -> None:
    """stdio has no URL and no host: the guard has nothing to say about it."""
    target = McpTarget(kind="stdio", command=[sys.executable, str(_SERVER_PATH)])
    assert {tool.name for tool in list_tools(target)} == EXPECTED_TOOLS


def test_a_url_is_still_required_before_the_host_is_checked() -> None:
    """Order matters for the message: an http target with no url reads as a
    usage error, not as a refused host."""
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(McpTarget(kind="http"))
    assert "needs a url" in str(excinfo.value)
