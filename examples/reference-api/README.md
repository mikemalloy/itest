# http_probe reference API

A small FastAPI app with **one route per branch** of the `http_probe` recipe
(`skills/itest-implementer/references/recipes/http_probe.md`), plus a harness
(`tests/test_reference_api.py`) that runs recipe-shaped probes against it and
asserts each branch's outcome. It is the recipe's regression guard and the safe
stage for demoing the critical-catch. Nothing here is deployed and nothing talks
to AWS.

## Route → branch table

| Route | Method | Recipe branch | Probe expectation | Outcome |
|---|---|---|---|---|
| `/secured/item` | GET | secured refusal | unauth → 401/403 | **PASS** |
| `/secured/item` | POST | unsafe secured refusal (bodyless) | unauth → 401/403 | **PASS** |
| `/public/info` | GET | public latency path (`security: []`) | 200 under the latency bound | **PASS** |
| `/health` | GET | health | 200 | **PASS** |
| `/secured/lookup/{id}` | GET | auth-ordering finding (lookup **before** auth) | unauth → 401/403 | **FAIL** — returns 404 |
| `/leaky/action` | POST | the deliberate hole | unauth → 401/403 | **FAIL == CRITICAL** — returns 2xx |
| `/secured/me` | GET | authenticated happy path | with token → 200 | **PASS** |

The two **FAIL** rows are the point, not a mistake. A recipe-shaped probe
asserting 401/403 against the auth-ordering route observes **404** (proof auth
ran only after the database lookup), and against the leaky route observes **2xx**
to an unauthenticated unsafe POST — the unauthenticated-unsafe-2xx condition the
recipe classifies as **CRITICAL**. A run in which every probe is green would be
broken: it would mean the catch branches never fired.

The app's `app.openapi()` declares per-operation `security` truthfully **except**
`/leaky/action`, whose spec claims a bearer requirement its handler does not
enforce. That spec-vs-enforcement gap is exactly what the sweep exposes.

## Running it

```sh
# Serve the app (from the repo root):
pip install -e '.[examples]'
uvicorn app:app --app-dir examples/reference-api
#   GET  http://127.0.0.1:8000/health        -> 200
#   POST http://127.0.0.1:8000/leaky/action  -> 200  (the hole; try it unauth)
#   GET  http://127.0.0.1:8000/openapi.json  -> the security-annotated schema

# Or run the branch-coverage harness, which stands the app up itself:
pip install -e '.[dev]'
pytest tests/test_reference_api.py tests/test_reference_app.py -q
```

## Why a reference app has to exist

Three of these branches cannot be shown on a real production system. You cannot
ship a deliberately unguarded mutating endpoint to prod to prove the CRITICAL
catch fires; the authenticated happy path needs a real credential baked into a
repeatable test, which prod auth is designed to prevent; and the public-latency
floor needs a response you can make slow on demand. A live prod sweep can only
ever show the *green* branches — the refusals holding. The red branches, the
ones that prove the recipe's catches actually catch, need a safe stage that is
allowed to be broken on purpose. That stage is this app.
