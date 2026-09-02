"""F4 regression: the HTTP probe must allow only http/https and refuse SSRF hosts.

The probe kept urllib's default handlers, so ``probe("file:///etc/passwd")`` read
a local file, and a base URL derived from untrusted Terraform state could point
at ``http://169.254.169.254/`` (cloud metadata) or loopback. The guard rejects a
non-http(s) scheme and a loopback/link-local/metadata host *before* any request,
with a typed error. Legitimate local targets (the reference app, the probe's own
acceptance server) opt in explicitly with ``allow_private_hosts=True``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from itest.probes import http as http_probe
from itest.probes.http import ProbeBlocked, probe


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://ftp.example.com/secret",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:8080/",  # loopback
        "http://[::1]:8080/",  # IPv6 loopback
        "http://localhost:8080/",  # loopback by name
    ],
)
def test_dangerous_url_raises_typed_error(url: str) -> None:
    with pytest.raises(ProbeBlocked):
        probe(url)


def test_blocked_url_makes_no_request(monkeypatch) -> None:
    """The refusal happens before the opener is ever built, so nothing is sent."""

    def explode(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("probe opened a connection despite the guard")

    monkeypatch.setattr(http_probe.urllib.request, "build_opener", explode)
    with pytest.raises(ProbeBlocked):
        probe("file:///etc/passwd")
    with pytest.raises(ProbeBlocked):
        probe("http://169.254.169.254/latest/meta-data/")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture
def loopback_server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_loopback_refused_by_default_allowed_on_opt_in(loopback_server: str) -> None:
    # Default: a loopback target is refused.
    with pytest.raises(ProbeBlocked):
        probe(f"{loopback_server}/")
    # Opt-in: a deliberate local target (the reference app) is reached.
    result = probe(f"{loopback_server}/", allow_private_hosts=True)
    assert result.status == 200


def test_public_host_passes_the_guard(monkeypatch) -> None:
    """A normal http(s) public host is not blocked (the request itself is stubbed
    so the test makes no real network call)."""
    from itest.probes.http import ProbeResult

    sentinel = ProbeResult(status=204, elapsed_ms=1.0)

    def fake_send(url, method, headers, timeout):
        return sentinel

    monkeypatch.setattr(http_probe, "_send", fake_send)
    assert probe("https://example.com/health") is sentinel
