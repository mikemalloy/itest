"""The HTTP probe: bodyless, redirect-recording, timeout-typed.

Exercised against a stdlib ``http.server`` bound to a loopback ephemeral port
— no external network, nothing to mock. The probe's whole reason to exist is to
ask "does the guard hold?" in the least dangerous way possible, so the tests
pin exactly that: it captures a status (including the 401/403 it is built to
look for), records a redirect's Location without following it, times out into a
typed error carrying the elapsed time, and sends neither a body nor any
authorization header of its own.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from itest.probes.http import ProbeResult, ProbeTimeout
from itest.probes.http import probe as _probe


def probe(url, **kwargs):
    """The probe against this file's loopback server, opted in to reach it.

    These tests exercise the probe's mechanics (bodyless, no-redirect, timeout)
    against a deliberate local server, so they pass allow_private_hosts=True.
    The host guard itself is covered in test_probe_http_guard.py.
    """
    kwargs.setdefault("allow_private_hosts", True)
    return _probe(url, **kwargs)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the test output quiet
        pass

    def _record(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.recorded.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        return body

    def _dispatch(self) -> None:
        self._record()
        if self.path == "/ok":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/secure":
            self.send_response(401)
            self.end_headers()
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
        elif self.path == "/login":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"login page")
        elif self.path == "/slow":
            time.sleep(2.0)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    # Every method routes through the same dispatcher so unsafe methods ride
    # the same bodyless path a GET does.
    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch


@pytest.fixture
def server() -> Iterator[tuple[str, list[dict]]]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.recorded = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}", httpd.recorded  # type: ignore[attr-defined]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_captures_a_200_status(server) -> None:
    base, _ = server
    result = probe(f"{base}/ok")
    assert isinstance(result, ProbeResult)
    assert result.status == 200
    assert result.redirect_location is None


def test_captures_a_401_without_raising(server) -> None:
    """The point of the probe: an unauthenticated request that is refused is a
    result to report, not an exception to handle."""
    base, _ = server
    result = probe(f"{base}/secure")
    assert result.status == 401


def test_latency_is_recorded_and_sane(server) -> None:
    base, _ = server
    result = probe(f"{base}/ok")
    assert result.elapsed_ms >= 0.0
    assert result.elapsed_ms < 5000.0


def test_redirect_is_recorded_not_followed(server) -> None:
    base, recorded = server
    result = probe(f"{base}/redirect")
    assert result.status == 302
    assert result.redirect_location == "/login"
    # Proof it did not follow: the server never saw a request for /login.
    assert not any(r["path"] == "/login" for r in recorded)


def test_timeout_raises_typed_error_carrying_elapsed(server) -> None:
    base, _ = server
    start = time.monotonic()
    with pytest.raises(ProbeTimeout) as excinfo:
        probe(f"{base}/slow", timeout=0.3)
    waited_ms = (time.monotonic() - start) * 1000
    # It gave up near the limit, not after the handler's full 2s sleep.
    assert excinfo.value.elapsed_ms > 0.0
    assert waited_ms < 1500.0


def test_sends_no_body_and_no_auth_header(server) -> None:
    """An unsafe method rides the sweep bodyless, with no auth of the probe's
    own — the least dangerous way to ask whether auth stops the request."""
    base, recorded = server
    probe(f"{base}/echo", method="POST")
    sent = next(r for r in recorded if r["path"] == "/echo")
    assert sent["method"] == "POST"
    assert sent["body"] == b""
    assert "authorization" not in sent["headers"]
    assert "cookie" not in sent["headers"]


def test_passes_caller_headers_through(server) -> None:
    """Headers the caller sets are sent; the probe simply adds none of its own
    auth. This is how a recipe's authenticated happy-path test supplies a
    customer credential when one is available."""
    base, recorded = server
    probe(f"{base}/ok", headers={"Authorization": "Bearer test-token"})
    sent = next(r for r in recorded if r["path"] == "/ok")
    assert sent["headers"]["authorization"] == "Bearer test-token"
