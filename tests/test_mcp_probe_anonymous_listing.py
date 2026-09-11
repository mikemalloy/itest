"""``list_tools(anonymous=True)`` and ``McpProbeError.refused``.

Two small additions the check library needs (``itest/checks``, A1):

- A1 records whether a server hands its tool listing to an anonymous caller.
  ``list_tools`` always used the target's credential when it could resolve one,
  and clearing ``credential_env`` is not the same as anonymous: over stdio the
  subprocess would then inherit an *ambient* token from the parent environment.
  ``anonymous=True`` keeps the name so the transport strips it, and supplies
  nothing.
- A refused listing (HTTP 401/403) and a server that was not there are both an
  ``McpProbeError``; ``refused`` tells them apart without parsing the message.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from itest.probes.mcp import McpProbeError, McpTarget, list_tools

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "reference-mcp" / "server.py"
)
_spec = importlib.util.spec_from_file_location("reference_mcp_server", _SERVER_PATH)
assert _spec and _spec.loader
reference_mcp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reference_mcp
_spec.loader.exec_module(reference_mcp)

TOKEN = "anon-listing-token-7f3e-do-not-log"
ENV = "ITEST_ANON_LISTING_TOKEN"

# A stdio server whose one tool's description says whether the credential
# variable reached the subprocess.
_ENV_REPORTER = textwrap.dedent(
    f"""
    import os
    from mcp.server.mcpserver import MCPServer

    present = bool(os.environ.get({ENV!r}))
    server = MCPServer(name="env-reporter", version="1.0.0")

    @server.tool(name="report", description=f"token-present={{present}}")
    def report() -> str:
        return "ok"

    server.run("stdio")
    """
)


@pytest.fixture(scope="module")
def reference() -> Iterator[Any]:
    with reference_mcp.serve_in_thread(token=TOKEN) as running:
        yield running


@pytest.fixture
def reporter(tmp_path: Path) -> list[str]:
    script = tmp_path / "env_reporter.py"
    script.write_text(_ENV_REPORTER, encoding="utf-8")
    return [sys.executable, str(script)]


def test_anonymous_listing_on_the_guarded_mount_is_refused(
    reference: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, TOKEN)
    target = McpTarget(
        kind="http",
        url=reference.guarded_url,
        credential_env=ENV,
        allow_private_hosts=True,
    )
    # With the credential: listed.
    assert len(list_tools(target)) == 8
    # Anonymous, though the credential is resolvable: refused.
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(target, anonymous=True)
    assert excinfo.value.refused is True
    assert TOKEN not in str(excinfo.value)


def test_anonymous_listing_on_the_open_mount_is_admitted(reference: Any) -> None:
    target = McpTarget(
        kind="http",
        url=reference.open_url,
        credential_env=ENV,
        allow_private_hosts=True,
    )
    assert len(list_tools(target, anonymous=True)) == 8


def test_anonymous_stdio_listing_strips_an_ambient_credential(
    reporter: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, TOKEN)
    target = McpTarget(kind="stdio", command=reporter, credential_env=ENV)

    (authed,) = list_tools(target)
    assert authed.description == "token-present=True"

    (anonymous,) = list_tools(target, anonymous=True)
    assert anonymous.description == "token-present=False"


def test_a_transport_failure_is_not_a_refusal() -> None:
    target = McpTarget(kind="stdio", command=[sys.executable, "-c", "pass"])
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(target, anonymous=True)
    assert excinfo.value.refused is False


def test_a_usage_error_is_not_a_refusal() -> None:
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(McpTarget(kind="http"), anonymous=True)
    assert excinfo.value.refused is False
