"""Branch-coverage harness for the http_probe recipe.

Stands the reference app (``examples/reference-api/app.py``) up under uvicorn on
a loopback ephemeral port — the way the probe's own acceptance stands up a
stdlib server — then runs recipe-shaped probe functions built on
``itest.probes.http`` against it and asserts an EXPECTED-OUTCOME TABLE.

The central subtlety: some branches are proven by a RED result. The
auth-ordering route answers 404 to an unauthenticated probe, and the leaky route
answers 2xx to an unauthenticated unsafe POST. A recipe-shaped probe asserting
401/403 against either MUST FAIL — and those two failures, one classified
CRITICAL, are the proof that the catch branches fire. A harness in which every
probe is green would be broken. So the two reds are asserted to be red, and red
for the RIGHT reason: the lookup probe by its observed 404, the leaky probe by
its observed 2xx.

The last section proves the FULL CHAIN, not just the bare probe: it drives the
probes through the recipe's real base-URL resolution — a synthetic
``terraform show -json`` state (written at test time, once the loopback port is
known) is walked by ITest's own ``iter_resources``/``detect_all``, a route_edge
is detected, and the base URL is resolved FROM that state, never passed in. That
is the plumbing that makes this a Terraform tool rather than a generic API
checker.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from itest.core.detectors.base import detect_all, iter_resources
from itest.probes.credential import resolve_credential
from itest.probes.http import ProbeResult
from itest.probes.http import probe as _probe


def probe(url, **kwargs):
    """The probe against the loopback reference app, opted in to reach it.

    The reference app is a deliberate local target, so every probe here passes
    allow_private_hosts=True — the SSRF guard (test_probe_http_guard.py) still
    protects a probe driven from real Terraform state, which never points at
    loopback.
    """
    kwargs.setdefault("allow_private_hosts", True)
    return _probe(url, **kwargs)


# --- load the reference app by path (its directory name is not importable) ---

_APP_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "reference-api" / "app.py"
)
_spec = importlib.util.spec_from_file_location("reference_api_app", _APP_PATH)
assert _spec and _spec.loader
reference_api_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference_api_app)

app = reference_api_app.app
TEST_TOKEN = reference_api_app.TEST_TOKEN

#: The recipe's default latency bound for public/health operations.
LATENCY_BOUND_MS = 2000.0


# --- recipe-shaped classification helpers (tested directly, §below) ----------

#: HTTP methods that do not mutate. Everything else is "unsafe" in the sense the
#: recipe means: an unauthenticated 2xx to one of them is an anonymous mutation.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_unsafe_method(method: str) -> bool:
    return method.upper() not in SAFE_METHODS


def classify_red(method: str, status: int) -> str:
    """Classify a recipe-red result.

    The recipe calls one case CRITICAL: an unauthenticated *unsafe* method that
    answered 2xx — an anonymous caller can mutate. Every other red (a secured
    GET that answered 2xx, a refusal that returned the wrong non-2xx code) is a
    finding to report, but not the stop-the-run emergency.
    """
    if is_unsafe_method(method) and 200 <= status < 300:
        return "CRITICAL"
    return "FINDING"


def assert_under_latency_bound(result: ProbeResult, bound_ms: float) -> None:
    """The recipe's public-latency assertion, factored out so it can be proven
    to actually assert. Raises AssertionError when the response is over bound."""
    assert result.elapsed_ms < bound_ms, (
        f"public endpoint took {result.elapsed_ms:.0f}ms, over the "
        f"{bound_ms:.0f}ms bound."
    )


# --- recipe-shaped probe functions (built on the real itest probe) -----------


def probe_requires_auth(base: str, path: str, method: str = "GET") -> ProbeResult:
    """Secured operation: an unauthenticated probe must be refused (401/403)."""
    result = probe(f"{base}{path}", method=method)
    assert result.status in (401, 403), (
        f"{method} {path} answered {result.status} unauthenticated; the guard "
        "did not hold. Expected 401 or 403."
    )
    return result


def probe_public(
    base: str, path: str, bound_ms: float = LATENCY_BOUND_MS
) -> ProbeResult:
    """Public operation: expect 200 under the latency bound."""
    result = probe(f"{base}{path}")
    assert result.status == 200, f"{path} answered {result.status}, not 200."
    assert_under_latency_bound(result, bound_ms)
    return result


def probe_health(base: str, path: str = "/health") -> ProbeResult:
    result = probe(f"{base}{path}")
    assert result.status == 200, f"{path} answered {result.status}, not 200."
    return result


def probe_authenticated(base: str, path: str, token: str) -> ProbeResult:
    """Happy path: with a valid credential the secured operation answers 200."""
    result = probe(f"{base}{path}", headers={"Authorization": f"Bearer {token}"})
    assert result.status == 200, (
        f"{path} answered {result.status} with a valid credential, not 200."
    )
    return result


# --- the loopback server ------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ThreadedUvicorn:
    """Run a uvicorn server in a daemon thread, the way the probe's acceptance
    stands up its stdlib ``http.server``: bound to loopback, no external
    network, torn down at the end."""

    def __init__(self, application, host: str, port: int) -> None:
        config = uvicorn.Config(application, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start within 10s")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    host, port = "127.0.0.1", _free_port()
    server = _ThreadedUvicorn(app, host, port)
    server.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()


# --- the expected-outcome table ----------------------------------------------
#
#   route                 probe                          expected
#   /secured/item GET     unauth expect 401/403          PASS
#   /secured/item POST    unauth bodyless expect 401/403 PASS
#   /public/info GET      expect 200 + elapsed<bound     PASS
#   /health GET           expect 200                     PASS
#   /secured/lookup/{id}  unauth expect 401/403          FAIL (returns 404)
#   /leaky/action POST    unauth bodyless expect 401/403 FAIL == CRITICAL (2xx)
#   /secured/me GET       with token expect 200          PASS

# (path, method, kind, expected_outcome)
EXPECTED_TABLE = [
    ("/secured/item", "GET", "requires_auth", "PASS"),
    ("/secured/item", "POST", "requires_auth", "PASS"),
    ("/public/info", "GET", "public_latency", "PASS"),
    ("/health", "GET", "health", "PASS"),
    ("/secured/lookup/does-not-exist", "GET", "requires_auth", "FAIL"),
    ("/leaky/action", "POST", "requires_auth", "FAIL"),
    ("/secured/me", "GET", "authenticated", "PASS"),
]


def _run_row(base: str, path: str, method: str, kind: str) -> str:
    """Run one recipe-shaped probe and return "PASS" or "FAIL"."""
    try:
        if kind == "requires_auth":
            probe_requires_auth(base, path, method=method)
        elif kind == "public_latency":
            probe_public(base, path)
        elif kind == "health":
            probe_health(base, path)
        elif kind == "authenticated":
            probe_authenticated(base, path, TEST_TOKEN)
        else:  # pragma: no cover - guards against a typo in the table
            raise ValueError(f"unknown probe kind: {kind}")
    except AssertionError:
        return "FAIL"
    return "PASS"


@pytest.mark.parametrize(("path", "method", "kind", "expected"), EXPECTED_TABLE)
def test_expected_outcome_table(
    base_url: str, path: str, method: str, kind: str, expected: str
) -> None:
    """Every branch behaves exactly as the recipe claims — passes pass, and the
    two intended reds fail. A green-everywhere run here would be BROKEN."""
    assert _run_row(base_url, path, method, kind) == expected


# --- the two reds, red for the RIGHT reason ----------------------------------


def test_lookup_red_is_a_404_auth_ordering_finding(base_url: str) -> None:
    """The lookup probe fails, and its observed status is 404 — proof the auth
    check ran only AFTER the database lookup (the anti-pattern the recipe
    catches). A correctly-ordered guard would have returned 401 first."""
    observed = probe(f"{base_url}/secured/lookup/does-not-exist")
    assert observed.status == 404
    # The recipe-shaped assertion therefore fails (it expected 401/403).
    with pytest.raises(AssertionError):
        probe_requires_auth(base_url, "/secured/lookup/does-not-exist")


def test_leaky_red_is_a_critical_unauth_unsafe_2xx(base_url: str) -> None:
    """The leaky probe fails, and its observed status is 2xx to an unauthenticated
    unsafe POST — the unauthenticated-unsafe-2xx condition the recipe calls
    CRITICAL. An anonymous caller can mutate."""
    observed = probe(f"{base_url}/leaky/action", method="POST")
    assert 200 <= observed.status < 300
    assert classify_red("POST", observed.status) == "CRITICAL"
    # The recipe-shaped assertion therefore fails (it expected 401/403).
    with pytest.raises(AssertionError):
        probe_requires_auth(base_url, "/leaky/action", method="POST")


# --- the classify helper, tested directly ------------------------------------


@pytest.mark.parametrize(
    ("method", "status", "expected"),
    [
        ("POST", 200, "CRITICAL"),
        ("DELETE", 204, "CRITICAL"),
        ("PUT", 201, "CRITICAL"),
        ("PATCH", 200, "CRITICAL"),
        ("GET", 200, "FINDING"),  # a safe-method 2xx is a finding, not CRITICAL
        ("HEAD", 200, "FINDING"),
        ("POST", 401, "FINDING"),  # a refusal is not a red at all
        ("POST", 403, "FINDING"),
        ("DELETE", 404, "FINDING"),
    ],
)
def test_classify_red(method: str, status: int, expected: str) -> None:
    assert classify_red(method, status) == expected


def test_is_unsafe_method() -> None:
    assert is_unsafe_method("post")
    assert is_unsafe_method("DELETE")
    assert not is_unsafe_method("get")
    assert not is_unsafe_method("HEAD")


# --- the public-latency assertion actually asserts ---------------------------


def test_latency_bound_trips_on_a_slow_response() -> None:
    """Force a slow response and prove the bound trips — otherwise the
    public-latency branch would be asserting nothing."""
    slow = ProbeResult(status=200, elapsed_ms=5000.0)
    with pytest.raises(AssertionError):
        assert_under_latency_bound(slow, LATENCY_BOUND_MS)


def test_latency_bound_passes_a_fast_response() -> None:
    fast = ProbeResult(status=200, elapsed_ms=12.0)
    assert_under_latency_bound(fast, LATENCY_BOUND_MS)  # does not raise


# --- the full chain: base URL resolved FROM STATE, never passed in -----------


def _synthetic_state(live_base: str) -> dict:
    """A ``terraform show -json`` state carrying a v2 route_edge whose API's
    ``api_endpoint`` is the loopback app. Shaped exactly like real state so
    ITest's own detectors and walker read it unmodified."""
    return {
        "format_version": "1.0",
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_apigatewayv2_api.reference",
                        "mode": "managed",
                        "type": "aws_apigatewayv2_api",
                        "name": "reference",
                        "values": {"id": "api-ref", "api_endpoint": live_base},
                    },
                    {
                        "address": "aws_apigatewayv2_integration.leaky",
                        "mode": "managed",
                        "type": "aws_apigatewayv2_integration",
                        "name": "leaky",
                        "values": {
                            "api_id": "api-ref",
                            "id": "int-ref",
                            "integration_type": "HTTP_PROXY",
                            "integration_uri": f"{live_base}/leaky/action",
                        },
                    },
                    {
                        "address": "aws_apigatewayv2_route.leaky",
                        "mode": "managed",
                        "type": "aws_apigatewayv2_route",
                        "name": "leaky",
                        "values": {
                            "api_id": "api-ref",
                            "route_key": "POST /leaky/action",
                            "target": "integrations/int-ref",
                            "authorization_type": "NONE",
                        },
                    },
                    {
                        "address": "aws_apigatewayv2_stage.default",
                        "mode": "managed",
                        "type": "aws_apigatewayv2_stage",
                        "name": "default",
                        "values": {"api_id": "api-ref", "name": "$default"},
                    },
                ]
            }
        },
    }


def resolve_base_url_from_state(
    state_json: dict, api_address: str, stage: str = "$default"
) -> str:
    """The recipe's base-URL resolution (§3), over ITest's own state walker.

    Finds the API resource by HCL address in ``terraform show -json`` output and
    reads its endpoint: ``api_endpoint`` for a v2 API (a named stage appended),
    ``invoke_url`` for a v1 stage. Nothing is pasted — the URL comes from state.
    """
    for resource in iter_resources(state_json):
        if resource.get("address") != api_address:
            continue
        values = resource.get("values", {}) or {}
        if values.get("api_endpoint"):  # v2 aws_apigatewayv2_api
            base = values["api_endpoint"].rstrip("/")
            return base if stage == "$default" else f"{base}/{stage}"
        if values.get("invoke_url"):  # v1 aws_api_gateway_stage
            return values["invoke_url"].rstrip("/")
        raise LookupError(f"{api_address} has neither api_endpoint nor invoke_url")
    raise LookupError(f"{api_address} is not in the state")


def test_state_carries_a_detectable_route_edge(base_url: str, tmp_path: Path) -> None:
    """ITest's real detectors find a route_edge in the synthetic state, and its
    source is the API we will resolve the base URL from."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_synthetic_state(base_url)), encoding="utf-8")
    state = json.loads(state_file.read_text(encoding="utf-8"))

    points, _ = detect_all(state)
    route_edges = [p for p in points if p.type == "route_edge"]
    assert route_edges, "expected a route_edge in the synthetic state"
    assert any(p.source == "aws_apigatewayv2_api.reference" for p in route_edges)


def test_probe_drives_through_state_resolved_base_url(
    base_url: str, tmp_path: Path
) -> None:
    """The full chain: state on disk -> ITest walks it -> base URL resolved FROM
    state -> probe. The resolved base equals the live app (dynamic port and
    all), so the resolution is exercised, not bypassed."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_synthetic_state(base_url)), encoding="utf-8")
    state = json.loads(state_file.read_text(encoding="utf-8"))

    resolved = resolve_base_url_from_state(state, "aws_apigatewayv2_api.reference")
    assert resolved == base_url  # resolved from state, not the fixture value

    # And the recipe branches still behave when driven through the resolved base.
    assert probe_health(resolved).status == 200
    leaky = probe(f"{resolved}/leaky/action", method="POST")
    assert 200 <= leaky.status < 300
    assert classify_red("POST", leaky.status) == "CRITICAL"


def test_resolution_raises_when_address_absent(base_url: str) -> None:
    """Proof the resolver really reads state: an address that is not present is
    a LookupError, not a silently-pasted URL."""
    state = _synthetic_state(base_url)
    with pytest.raises(LookupError):
        resolve_base_url_from_state(state, "aws_apigatewayv2_api.nonexistent")


# --- the credential path: env-file -> resolver -> authenticated probe --------

#: A distinct env-var NAME for these tests, so the token is never a literal in
#: the probe path — it is written to a temp .itest/.env and read back by name.
CREDENTIAL_ENV = "ITEST_REFERENCE_APP_TOKEN"


@pytest.fixture
def restore_environ() -> Iterator[None]:
    """resolve_credential seeds os.environ from .itest/.env; snapshot/restore."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_env_file_credential_drives_an_authenticated_200(
    base_url: str, tmp_path: Path, restore_environ: None
) -> None:
    """The whole path: the token is written to a gitignored-style .itest/.env,
    read back by name via resolve_credential, and used to authenticate a probe
    against the secured happy-path route — 200, with no token hardcoded in the
    probe call."""
    os.environ.pop(CREDENTIAL_ENV, None)
    itest = tmp_path / ".itest"
    itest.mkdir()
    (itest / ".env").write_text(f"{CREDENTIAL_ENV}={TEST_TOKEN}\n", encoding="utf-8")

    token = resolve_credential(CREDENTIAL_ENV, tmp_path)
    assert token == TEST_TOKEN  # came from the env file, not a literal here

    result = probe_authenticated(base_url, "/secured/me", token)
    assert result.status == 200


def test_absent_env_file_yields_none_and_no_probe_is_attempted(
    base_url: str, tmp_path: Path, restore_environ: None
) -> None:
    """With no .itest/.env and the var unset, resolution is None — and the recipe
    contract is that no authenticated probe is generated or attempted."""
    os.environ.pop(CREDENTIAL_ENV, None)
    token = resolve_credential(CREDENTIAL_ENV, tmp_path)
    assert token is None

    # None -> not generated. Model the contract: the probe is never called.
    if token is None:
        outcome = "not-generated"
    else:  # pragma: no cover - the point is that this branch does not run
        outcome = probe_authenticated(base_url, "/secured/me", token).status
    assert outcome == "not-generated"
