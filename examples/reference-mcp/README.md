# MCP probe reference server

A small MCP server with **one tool per branch** of the MCP probe's reasoning
(`itest/probes/mcp.py`), plus a harness (`tests/test_reference_mcp.py`) that
pins its shape. It is to the MCP probe what `examples/reference-api/` is to the
HTTP probe: a server that is *allowed to be broken on purpose*, so the branches
a real deployment must never show can be exercised locally. Nothing here is
deployed, nothing talks to AWS, and nothing egresses.

Built on the official Python MCP SDK (`mcp` on PyPI, 2.x), using
`mcp.server.mcpserver.MCPServer` — the class the SDK's 1.x line called
`FastMCP`.

## Tool → branch table

| Tool | Annotations | Mutation class | Branch it proves |
|---|---|---|---|
| `get_guide()` | *(none)* | informational | No hints and no parameters — only the **name** can classify it |
| `search_records(query)` | `readOnlyHint=true` | read | A read with free-form input (no enum, no pattern) |
| `fetch_record(id)` | `readOnlyHint=true` | read | A 404-shaped tool error (`isError=true`) for an unknown id |
| `create_record(name)` | `readOnlyHint=false`, `destructiveHint=false` | write | The plain additive write |
| `update_record(id, name)` | `readOnlyHint=false`, `destructiveHint=false` | write | A write against an id that may not exist |
| `delete_record(id, confirm)` | `destructiveHint=true` | destructive | The destructive tool, behind a `confirm` gate |
| `lookalike_read(id)` | `readOnlyHint=true` | **read (a lie)** | **Annotation conflict** — declared read-only, mutates anyway |
| `enrich(email)` | `readOnlyHint=true`, `openWorldHint=true` | read | *Describes* an external provider; opens no socket |

## The two deliberate defects

**The open mount.** The server is served twice: guarded by a bearer token at
`/mcp`, and with **no check at all** at `/open/mcp`. The open mount is how an
unauthenticated `tools/call` on a *mutating* tool can be shown reaching a
server — the MCP analogue of the reference API's `/leaky/action`. A run in which
the open mount refused an anonymous caller would be broken: the bait would be
gone.

**`lookalike_read`.** It declares `readOnlyHint=true`, its name reads like a
read, and it increments a counter on the record anyway. The conflict is
**behavioral**, not declarative: the annotation and the name agree with each
other, and both are wrong. No static reading of `tools/list` can catch it — only
calling it and watching the store can. Do not "fix" it; that catch is a later
recipe's job, and this server is where it will be regression-tested.

One more load-bearing detail: **`delete_record` never raises.** A miss is
reported in its payload, so a call with a *sentinel* id (one that cannot exist)
returns a **successful** result. That is what lets the "an anonymous caller
reached a destructive tool" catch fire without anything being destroyed to prove
it.

## Running it

```sh
pip install -e '.[examples]'

# Over stdio (a subprocess speaking JSON-RPC on stdin/stdout):
python examples/reference-mcp/server.py

# Over streamable HTTP:
REFERENCE_MCP_TOKEN=reference-token python examples/reference-mcp/server.py --http --port 8000
#   POST http://127.0.0.1:8000/mcp       -> 401 without the bearer token
#   POST http://127.0.0.1:8000/open/mcp  -> served to anyone (the hole)
```

The directory name has a hyphen, so it is not an importable package; the
harnesses load `server.py` by path, exactly as `tests/test_reference_api.py`
loads the reference API. `serve_in_thread()` in that file stands the HTTP form
up on a loopback ephemeral port and hands back the live record store, which is
what lets a harness assert a mutation *directly* rather than inferring one from
a later read.

The bearer token comes from `REFERENCE_MCP_TOKEN`, defaulting to
`reference-token` when unset. That constant is an example's convenience, not a
product default — this server exists to be probed on loopback.

## Running ITest against it

The directory is an ITest project as shipped: a declaration
(`.itest/tools/reference-mcp.yaml`) and the environment policy it needs
(`.itest/environments.yaml`). There is no Terraform here, and none is needed —
a declarations-only project plans its tools without it. From this directory,
with ITest installed (`pip install -e '.[examples]'` at the repo root):

```sh
export REFERENCE_MCP_TOKEN=dry-run-token   # a name the declaration reads; any value
itest plan                                  # eight tools, no --tf-json
itest sync                                  # engine modules, bindings, your conftest.py
itest verify --environment staging          # staging permits the active tier
itest report --html --out readiness.html
```

The declaration's `[python, server.py]` launches `server.py` from this
directory under the interpreter ITest runs in, whatever directory you start
from. Nothing generated here is meant to be committed: `.itest/manifest.yaml`,
`.itest/plan.json` and `itest_tests/` are what a real project would keep, and
here they are output from trying it out.

What to expect, and why: over stdio this server has no guard at all, so `authority.anonymous`
(refuses anonymous) **fails** on its five read tools and is `not_verifiable` on
the three mutating ones — the page reads BLOCKED, which is the truth about an
unauthenticated stdio server. The two deliberate defects are not caught by
this run: the open mount exists only over HTTP, and a declaration cannot opt
into a loopback URL; `lookalike_read` passes the readonly mutation-class check
by design and waits on the active-tier "mutation class observed" check.

## Why a reference server has to exist

Three of these branches cannot be shown against a real MCP server. You cannot
ship an unauthenticated mount exposing a destructive tool to prove the
unauthenticated-mutating-call catch fires; you cannot rely on someone else's
server to keep declaring a hint that contradicts its behaviour; and you cannot
make a third party's tool hang on demand to prove a timeout is a distinct
outcome from a refusal. A probe run against a real server can only ever show the
*green* branches — the guards holding. The red branches, the ones that prove the
catches actually catch, need a stage that is allowed to be broken on purpose.
That stage is this server.
