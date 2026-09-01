"""Tests for the reference API itself (not the probe).

The reference app under ``examples/reference-api/`` has one route per branch of
the http_probe recipe. Here we pin each route's status with and without the
bearer token, using Starlette's ``TestClient`` — this exercises the app, not the
probe (that is the harness in ``test_reference_api.py``). The point is that the
app really does what its docstrings claim: the secured routes refuse, the public
ones answer, the auth-ordering route returns 404-before-401, and the leaky route
is genuinely unguarded while its OpenAPI spec claims otherwise.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_APP_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "reference-api" / "app.py"
)
_spec = importlib.util.spec_from_file_location("reference_api_app", _APP_PATH)
assert _spec and _spec.loader
reference_api_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference_api_app)

app = reference_api_app.app
TEST_TOKEN = reference_api_app.TEST_TOKEN


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


# -- secured refusal (GET) ------------------------------------------------


def test_secured_item_get_refuses_without_token(client: TestClient) -> None:
    assert client.get("/secured/item").status_code == 401


def test_secured_item_get_allows_with_token(client: TestClient) -> None:
    assert client.get("/secured/item", headers=_auth()).status_code == 200


# -- unsafe secured refusal (POST) ----------------------------------------


def test_secured_item_post_refuses_without_token(client: TestClient) -> None:
    assert client.post("/secured/item").status_code == 401


def test_secured_item_post_allows_with_token(client: TestClient) -> None:
    assert client.post("/secured/item", headers=_auth()).status_code == 200


# -- public + health ------------------------------------------------------


def test_public_info_is_open(client: TestClient) -> None:
    assert client.get("/public/info").status_code == 200


def test_health_is_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


# -- auth-ordering anti-pattern -------------------------------------------


def test_lookup_missing_id_returns_404_without_token(client: TestClient) -> None:
    """The anti-pattern: lookup runs before auth, so a missing id answers 404
    to an unauthenticated request instead of the 401 a correct guard gives."""
    assert client.get("/secured/lookup/does-not-exist").status_code == 404


def test_lookup_existing_id_returns_401_without_token(client: TestClient) -> None:
    """Proof the auth check exists but runs late: an id that *is* present makes
    the lookup succeed, and only then does the missing token surface as 401."""
    assert client.get("/secured/lookup/1").status_code == 401


def test_lookup_existing_id_returns_200_with_token(client: TestClient) -> None:
    assert client.get("/secured/lookup/1", headers=_auth()).status_code == 200


# -- the deliberate hole (CRITICAL) ---------------------------------------


def test_leaky_action_answers_200_to_anonymous_post(client: TestClient) -> None:
    """The hole: an anonymous, bodyless POST mutates and gets 200. The spec
    claims a security requirement (below); the handler enforces nothing."""
    assert client.post("/leaky/action").status_code == 200


# -- authenticated happy path ---------------------------------------------


def test_secured_me_refuses_without_token(client: TestClient) -> None:
    assert client.get("/secured/me").status_code == 401


def test_secured_me_allows_with_token(client: TestClient) -> None:
    assert client.get("/secured/me", headers=_auth()).status_code == 200


# -- OpenAPI: truthful except the leaky route -----------------------------


def test_openapi_declares_security_on_secured_routes(client: TestClient) -> None:
    schema = app.openapi()
    for path, method in [
        ("/secured/item", "get"),
        ("/secured/item", "post"),
        ("/secured/lookup/{item_id}", "get"),
        ("/secured/me", "get"),
    ]:
        assert schema["paths"][path][method]["security"] == [{"bearerAuth": []}]


def test_openapi_declares_public_routes_open(client: TestClient) -> None:
    schema = app.openapi()
    assert schema["paths"]["/public/info"]["get"]["security"] == []
    assert schema["paths"]["/health"]["get"]["security"] == []


def test_openapi_claims_security_leaky_route_does_not_enforce(
    client: TestClient,
) -> None:
    """The gap the sweep exposes: the spec says /leaky/action is secured, but a
    real anonymous POST proves it is not."""
    schema = app.openapi()
    assert schema["paths"]["/leaky/action"]["post"]["security"] == [{"bearerAuth": []}]
    assert client.post("/leaky/action").status_code == 200  # spec lied
