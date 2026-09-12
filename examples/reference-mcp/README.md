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

## Running ITest against it: the two-terminal demo

The directory is an ITest project as shipped: **two** declarations and the
environment policy they need (`.itest/environments.yaml`). There is no
Terraform here, and none is needed — a declarations-only project plans its
tools without it.

- `.itest/tools/reference-mcp.yaml` — the server over **stdio**, launched as a
  subprocess. Whoever can launch it is authorised, so this declaration gets no
  `authority.anonymous` check.
- `.itest/tools/reference-mcp-open.yaml` — the server's **unguarded HTTP
  mount**, declared with no auth scheme and `transport.allow_private_hosts:
  true` so the loopback url is accepted. A deliberately defective example, not
  a template: this is where an anonymous caller is a real thing, and where the
  anonymous-refusal check has something to catch.

With ITest installed (`pip install -e '.[examples]'` at the repo root), in
**terminal one**, from this directory:

```sh
python server.py --http --port 8000
#   POST http://127.0.0.1:8000/mcp       -> 401 without the bearer token
#   POST http://127.0.0.1:8000/open/mcp  -> served to anyone (the hole)
```

In **terminal two**, also from this directory:

```sh
export REFERENCE_MCP_TOKEN=dry-run-token                   # names, never values
export REFERENCE_MCP_OPEN_URL=http://127.0.0.1:8000/open/mcp
itest plan                                  # eight tools on each server, no --tf-json
itest sync                                  # engine modules, bindings, your conftest.py
itest verify --environment staging          # staging permits the active tier
itest report --html --environment staging --out readiness.html
itest verify --environment staging --output json | itest standards   # the OWASP lens
```

The plan names the open mount under "Private hosts allowed by declaration",
every time: the private-host guard is the SSRF rule, and a declaration that
loosens it must never be silent.

**Stdio only.** Without terminal one, `REFERENCE_MCP_OPEN_URL` is unset, so
plan reports `reference-mcp-open` as unreachable and `itest sync` refuses to
write until you accept the gap:

```sh
export REFERENCE_MCP_TOKEN=dry-run-token
itest sync --allow-unreachable              # the stdio server alone: eight tools
itest verify --environment staging
```

The stdio declaration's `[python, server.py]` launches `server.py` from this
directory under the interpreter ITest runs in, whatever directory you start
from. Nothing generated here is meant to be committed: `.itest/manifest.yaml`,
`.itest/plan.json` and `itest_tests/` are what a real project would keep, and
here they are output from trying it out.

### What each mount is expected to produce

**`reference-mcp` (stdio).** No `authority.anonymous` cell at all. The server
is a subprocess, so whoever can launch it is authorised — the process boundary
is the authentication boundary, and there is no anonymous caller to refuse.
The trait's rule (`transport.kind == http or auth.enforced_over_stdio`) keeps
it off a stdio server that does not claim a credential check of its own;
`itest traits --for reference-mcp/get_guide` prints `does not apply` with that
rule beside it. The listing checks (`blast.mutation_class`, the three `change`
checks) pass on every tool; the generated bindings wait on the fixtures in
your `conftest.py` and read `NOT RUN`; nothing fails.

**`reference-mcp-open` (the unguarded HTTP mount).** `authority.anonymous`
applies to every tool. The five read tools **fail** it — an anonymous
`tools/call` answered — and the three mutating tools are `not_verifiable`,
deferred to the active tier (a listing is not a call, and the readonly check
never says `critical`). The page reads **BLOCKED**, which is the truth about
an MCP server mounted without authentication, and every cell carries the
published id it answers (ASI03, CWE-306 …).

`lookalike_read` passes the readonly mutation-class check on both mounts by
design; only the active-tier observed check can catch it (see the tool table
above).

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
