"""A single-shot HTTP probe for active-tier endpoint checks.

The probe exists to answer one question — *does the guard hold?* — in the least
dangerous way available, and every rule here follows from that:

- **No request body, ever.** There is no ``body`` parameter. An unauthenticated
  probe of an unsafe method carrying an empty body is the safest possible way to
  ask "does auth stop me?": if a ``DELETE`` with no body is refused with 401, the
  guard held; if it is *accepted*, that is the finding, and no data had to be
  sent to discover it.
- **No redirects followed.** A 3xx is an answer, not a detour — a redirect to a
  login page is exactly how an unauthenticated request is turned away. The probe
  records the ``Location`` header and returns the 3xx status rather than chasing
  it to a second, unrelated endpoint.
- **No retries.** One request, one result. A retry would blur latency and could
  turn a single unsafe call into several.
- **No authorization of its own.** The probe adds no ``Authorization`` or
  ``Cookie`` header. A caller may pass headers explicitly (that is how an
  authenticated happy-path test supplies a supplied test credential), but the
  default request carries nothing that would make an endpoint trust it.
- **Timeout is a caller's parameter**, and exceeding it raises a typed
  :class:`ProbeTimeout` carrying the elapsed time — a hang is a distinct outcome
  from a refusal, and the caller must be able to tell them apart.
- **Only http/https, and no SSRF hosts.** The base URL is derived from
  ``terraform show -json`` — untrusted input. A ``file://`` scheme would read a
  local file, and a loopback / link-local / metadata host
  (``169.254.169.254``, ``::1``, ``127.0.0.0/8``, ``169.254.0.0/16``) is the
  classic SSRF target on a CI runner or cloud instance. Both are refused with a
  typed :class:`ProbeBlocked` *before any request is made*. A deliberate local
  target — the reference app under ``examples/``, or the probe's own acceptance
  server — opts in with ``allow_private_hosts=True``.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of one probe.

    ``redirect_location`` is the raw ``Location`` header of a 3xx response, or
    ``None`` when the response was not a redirect (or carried no Location).
    """

    status: int
    elapsed_ms: float
    redirect_location: str | None = None


class ProbeTimeout(Exception):
    """Raised when a probe exceeds its timeout. Carries the elapsed time."""

    def __init__(self, url: str, timeout: float, elapsed_ms: float) -> None:
        self.url = url
        self.timeout = timeout
        self.elapsed_ms = elapsed_ms
        super().__init__(
            f"probe of {url} timed out after {elapsed_ms:.1f}ms (limit {timeout}s)"
        )


class ProbeBlocked(Exception):
    """Raised, before any request, for a URL the probe refuses to send.

    Two reasons: a scheme other than http/https (a ``file://`` read is not a
    probe), or a loopback / link-local / metadata host that a base URL from
    untrusted state must not be able to steer the probe at. The latter is
    overridable with ``allow_private_hosts=True`` for a deliberate local target.
    """


_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _check_url(url: str, allow_private_hosts: bool) -> None:
    """Refuse a non-http(s) scheme or an SSRF host. Raises :class:`ProbeBlocked`.

    Runs before the opener is built, so a refused URL results in no request.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ProbeBlocked(
            f"probe refuses scheme {scheme or '(none)'!r} in {url!r}: only "
            "http and https are allowed (a base URL comes from untrusted state)."
        )
    if allow_private_hosts:
        return

    host = (parts.hostname or "").strip()
    if host.lower() == "localhost":
        raise ProbeBlocked(
            f"probe refuses loopback host in {url!r}. A base URL from state must "
            "not steer the probe at localhost; pass allow_private_hosts=True for "
            "a deliberate local target."
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a normal DNS name — not an SSRF literal
    # is_loopback covers 127.0.0.0/8 and ::1; is_link_local covers 169.254.0.0/16
    # (including the 169.254.169.254 metadata endpoint) and fe80::/10.
    if ip.is_loopback or ip.is_link_local:
        raise ProbeBlocked(
            f"probe refuses loopback/link-local/metadata host {host!r} in {url!r}. "
            "This is the classic SSRF target; pass allow_private_hosts=True only "
            "for a deliberate local target such as the reference app."
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to redirect.

    Returning ``None`` from ``redirect_request`` makes urllib stop rather than
    follow, and the 3xx surfaces as an ``HTTPError`` whose headers still carry
    the ``Location`` — which is precisely the value the probe wants to record.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def probe(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    allow_private_hosts: bool = False,
) -> ProbeResult:
    """Send one request and report its status, latency, and any redirect.

    Never sends a body, never follows a redirect, never retries, and adds no
    authorization header of its own. A non-2xx status (a 401/403 refusal, a 3xx
    redirect, a 404) is a normal result, not an error — only a timeout raises,
    as :class:`ProbeTimeout`. See the module docstring for why.

    Refuses a non-http(s) scheme or an SSRF host (loopback / link-local /
    metadata) with :class:`ProbeBlocked` before any request is made. A
    deliberate local target opts in with ``allow_private_hosts=True``.
    """
    _check_url(url, allow_private_hosts)
    return _send(url, method, headers, timeout)


def _send(
    url: str,
    method: str,
    headers: dict[str, str] | None,
    timeout: float,
) -> ProbeResult:
    """The network step, reached only after :func:`_check_url` allows the URL."""
    request = urllib.request.Request(url, method=method, headers=dict(headers or {}))
    opener = urllib.request.build_opener(_NoRedirect)

    start = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            location = response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        # Every non-2xx (including a not-followed 3xx) lands here. It is a
        # response, not a failure: read the status and Location and move on.
        status = exc.code
        location = exc.headers.get("Location")
        exc.close()
    except urllib.error.URLError as exc:
        # socket.timeout is an alias for TimeoutError on 3.11, so this one
        # isinstance covers a read/connect timeout surfaced via URLError.
        elapsed_ms = (time.monotonic() - start) * 1000
        if isinstance(exc.reason, TimeoutError):
            raise ProbeTimeout(url, timeout, elapsed_ms) from exc
        raise
    except TimeoutError as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        raise ProbeTimeout(url, timeout, elapsed_ms) from exc

    elapsed_ms = (time.monotonic() - start) * 1000
    return ProbeResult(status=status, elapsed_ms=elapsed_ms, redirect_location=location)
