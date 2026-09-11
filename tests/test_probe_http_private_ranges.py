"""The private-host guard covers RFC1918 and IPv6 unique-local, not just loopback.

P30's report flagged it: ``_check_url`` refused loopback, link-local and the
metadata endpoint, but a declaration's url pointing at ``10.0.0.5`` or
``192.168.1.1`` — an internal admin panel on the runner's network — sailed
through. The ranges come from :mod:`ipaddress` (``is_private``), never from
hand-written prefix strings.

A hostname is resolved and *every* address it resolves to is checked, so a DNS
name pointing at a private address is refused exactly as the literal would be.
The resolver is mocked throughout: nothing here depends on DNS.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from itest.probes import http as http_probe
from itest.probes.http import ProbeBlocked, ProbeResult, probe
from itest.probes.mcp import McpProbeError, McpTarget, list_tools

PRIVATE_LITERALS = [
    "http://10.0.0.5/",  # 10/8
    "http://10.255.255.254:8080/admin",
    "http://172.16.0.1/",  # 172.16/12, low end
    "http://172.31.255.254/",  # 172.16/12, high end
    "http://192.168.1.1/",  # 192.168/16
    "http://[fc00::1]/",  # fc00::/7, low half
    "http://[fd12:3456:789a::1]/",  # fc00::/7, the half actually used
    "http://[::ffff:10.0.0.5]/",  # an IPv4-mapped RFC1918 address
]


def _fake_getaddrinfo(mapping: dict[str, list[str]]):
    """A getaddrinfo that answers from a dict and fails like DNS otherwise."""

    def fake(host: str, *args: Any, **kwargs: Any) -> list[tuple]:
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "not in the test resolver")
        results = []
        for address in mapping[host]:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            results.append((family, socket.SOCK_STREAM, 6, "", (address, 0)))
        return results

    return fake


@pytest.fixture
def no_send(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the network step; record what would have been sent."""
    sent: list[str] = []

    def fake_send(url, method, headers, timeout):
        sent.append(url)
        return ProbeResult(status=204, elapsed_ms=1.0)

    monkeypatch.setattr(http_probe, "_send", fake_send)
    return sent


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch):
    """Install a fake resolver; returns a setter for its answers."""

    def install(mapping: dict[str, list[str]]) -> None:
        monkeypatch.setattr(
            http_probe.socket, "getaddrinfo", _fake_getaddrinfo(mapping)
        )

    install({})
    return install


@pytest.mark.parametrize("url", PRIVATE_LITERALS)
def test_private_ranges_are_refused_without_the_opt_in(
    url: str, no_send: list[str], resolver
) -> None:
    with pytest.raises(ProbeBlocked) as excinfo:
        probe(url)
    assert "allow_private_hosts" in str(excinfo.value)
    assert no_send == [], "the guard let a request through"


@pytest.mark.parametrize("url", PRIVATE_LITERALS)
def test_private_ranges_are_allowed_with_the_opt_in(
    url: str, no_send: list[str], resolver
) -> None:
    assert probe(url, allow_private_hosts=True).status == 204
    assert no_send == [url]


@pytest.mark.parametrize(
    "url", ["http://93.184.215.14/", "https://8.8.8.8/", "http://[2606:4700::1111]/"]
)
def test_a_public_address_still_proceeds(
    url: str, no_send: list[str], resolver
) -> None:
    assert probe(url).status == 204
    assert no_send == [url]


def test_a_hostname_resolving_to_a_private_address_is_refused(
    no_send: list[str], resolver
) -> None:
    resolver({"internal.corp.test": ["10.1.2.3"]})
    with pytest.raises(ProbeBlocked) as excinfo:
        probe("http://internal.corp.test/admin")
    message = str(excinfo.value)
    assert "internal.corp.test" in message
    assert no_send == []


def test_a_hostname_with_any_private_address_is_refused(
    no_send: list[str], resolver
) -> None:
    """Round-robin DNS can mix a public and a private answer. The connection
    could land on either, so one private answer is enough to refuse."""
    resolver({"mixed.example.test": ["93.184.215.14", "fd00::7"]})
    with pytest.raises(ProbeBlocked):
        probe("http://mixed.example.test/")
    assert no_send == []


def test_a_hostname_resolving_to_loopback_is_refused(
    no_send: list[str], resolver
) -> None:
    resolver({"sneaky.example.test": ["127.0.0.1"]})
    with pytest.raises(ProbeBlocked):
        probe("http://sneaky.example.test/")


def test_a_hostname_resolving_to_a_public_address_proceeds(
    no_send: list[str], resolver
) -> None:
    resolver({"api.example.test": ["93.184.215.14", "2606:4700::1111"]})
    assert probe("http://api.example.test/health").status == 204


def test_a_hostname_that_does_not_resolve_proceeds_to_the_transport(
    no_send: list[str], resolver
) -> None:
    """An unresolvable name is not a private host: the request step reports its
    own failure. Refusing it here would file "no such host" as an SSRF block."""
    assert probe("http://nowhere.invalid/").status == 204


def test_the_opt_in_skips_resolution_entirely(
    monkeypatch: pytest.MonkeyPatch, no_send: list[str]
) -> None:
    def explode(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("resolved a host despite allow_private_hosts")

    monkeypatch.setattr(http_probe.socket, "getaddrinfo", explode)
    assert probe("http://internal.corp.test/", allow_private_hosts=True).status == 204


def test_the_mcp_transport_inherits_the_wider_guard(
    monkeypatch: pytest.MonkeyPatch, resolver
) -> None:
    """The MCP probe imports this check, so RFC1918 is refused there too — and
    before the transport is built."""

    def explode(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("the MCP probe built a transport despite the guard")

    from itest.probes import mcp as mcp_probe

    monkeypatch.setattr(mcp_probe, "streamable_http_client", explode)
    resolver({"mcp.corp.test": ["192.168.4.20"]})
    for url in ("http://10.0.0.5/mcp", "http://mcp.corp.test/mcp"):
        with pytest.raises(McpProbeError) as excinfo:
            list_tools(McpTarget(kind="http", url=url))
        assert "refuses" in str(excinfo.value)
