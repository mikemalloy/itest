"""Reference API: one route per http_probe recipe branch.

This FastAPI app exists so every branch of the ``http_probe`` recipe
(``skills/itest-implementer/references/recipes/http_probe.md``) can be exercised
locally — including the branches a real production system must never safely
show: a deliberately unguarded mutating endpoint (the CRITICAL catch), an
authenticated happy path, and a measurable public-latency path. Each route below
names the recipe branch it proves; see ``README.md`` in this directory for the
route-to-branch table and the argument for why the app has to exist.

Nothing here is deployed and nothing talks to AWS. The app issues its own bearer
token from a constant (:data:`TEST_TOKEN`) rather than an identity provider, so
the authenticated happy-path branch is self-contained and reproducible.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.openapi.utils import get_openapi

#: The one valid bearer token. A constant, not an IdP: the reference app must
#: run with no external dependency for the happy-path branch to be provable.
TEST_TOKEN = "reference-api-test-token"

#: A tiny in-memory "database" for the auth-ordering branch. Only these ids
#: exist; every other id is a miss (404).
_ITEMS: dict[str, dict[str, str]] = {
    "1": {"id": "1", "name": "widget"},
}


def require_auth(authorization: str | None = Header(default=None)) -> str:
    """Bearer-token guard: a missing or wrong token raises 401.

    Wired as a FastAPI dependency on the secured routes, and also called
    *directly* (not as a dependency) by ``/secured/lookup`` to stage the
    auth-ordering anti-pattern — see that route.
    """
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization[len(prefix) :] != TEST_TOKEN:
        raise HTTPException(status_code=401, detail="invalid bearer token")
    return authorization[len(prefix) :]


app = FastAPI(title="ITest http_probe reference API", version="1.0.0")


@app.get("/secured/item")
def get_secured_item(_: str = Depends(require_auth)) -> dict[str, str]:
    """Branch: secured refusal. Require auth -> 401 without a token, 200 with."""
    return {"item": "secret"}


@app.post("/secured/item")
def post_secured_item(_: str = Depends(require_auth)) -> dict[str, str]:
    """Branch: unsafe secured refusal (bodyless). Require auth -> 401 without a token.

    An unauthenticated POST is probed with no body, so a refusal proves the
    guard without anything having been sent to act on.
    """
    return {"created": "ok"}


@app.get("/public/info")
def public_info() -> dict[str, str]:
    """Branch: public latency path. ``security: []`` -> 200, and meant to be fast."""
    return {"info": "public"}


@app.get("/health")
def health() -> dict[str, str]:
    """Branch: health -> 200."""
    return {"status": "ok"}


@app.get("/secured/lookup/{item_id}")
def secured_lookup(
    item_id: str, authorization: str | None = Header(default=None)
) -> dict[str, str]:
    """Branch: auth-ordering finding.

    INTENTIONAL ANTI-PATTERN — do not "fix" the ordering. The database lookup
    runs BEFORE the auth check, so an *unauthenticated* request for a missing id
    answers 404, not the 401 a correctly-ordered guard would return. That is
    exactly what the recipe's unauthenticated probe is built to catch: it
    expects 401/403 and observes 404, and the 404 proves auth ran only after the
    lookup. The reference app must keep the anti-pattern to prove the catch
    branch fires.
    """
    item = _ITEMS.get(item_id)  # lookup FIRST (the anti-pattern)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    require_auth(authorization)  # auth checked only AFTER the lookup
    return item


@app.post("/leaky/action")
def leaky_action() -> dict[str, str]:
    """Branch: CRITICAL. The deliberate hole — NO auth dependency at all.

    The OpenAPI document (see :func:`custom_openapi`) claims this operation
    requires a bearer token, but the handler enforces nothing: an anonymous,
    bodyless POST mutates and gets 200. A recipe-shaped probe asserting 401/403
    here MUST FAIL, and that failure — an unsafe method answering 2xx
    unauthenticated — is the unauthenticated-unsafe-2xx condition the recipe
    classifies as CRITICAL. The spec-vs-enforcement gap is the finding the
    sweep exposes; never relax the probe's assertion to 2xx to make it green.
    """
    return {"action": "performed"}


@app.get("/secured/me")
def secured_me(_: str = Depends(require_auth)) -> dict[str, str]:
    """Branch: authenticated happy path. Require auth -> 200 WITH a valid token."""
    return {"user": "me"}


# Operations whose OpenAPI ``security`` we set by hand. FastAPI does not infer a
# security requirement from the ``require_auth`` dependency, so the schema would
# otherwise be silent about which routes are guarded — and the recipe dispatches
# on exactly that field. We declare it truthfully, with one deliberate exception.
_SECURED_OPS = {
    ("/secured/item", "get"),
    ("/secured/item", "post"),
    ("/secured/lookup/{item_id}", "get"),
    ("/secured/me", "get"),
    # The deliberate lie: /leaky/action's spec claims a bearer requirement its
    # handler does NOT enforce. The gap between this line and leaky_action above
    # is precisely what the sweep exposes.
    ("/leaky/action", "post"),
}
#: Operations that explicitly opt out of security (``security: []``).
_PUBLIC_OPS = {
    ("/public/info", "get"),
    ("/health", "get"),
}


def custom_openapi() -> dict[str, Any]:
    """Serve an OpenAPI document that declares per-operation ``security``.

    Truthful everywhere except ``/leaky/action`` (see :data:`_SECURED_OPS`).
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
    }
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            key = (path, method.lower())
            if key in _SECURED_OPS:
                operation["security"] = [{"bearerAuth": []}]
            elif key in _PUBLIC_OPS:
                operation["security"] = []
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
