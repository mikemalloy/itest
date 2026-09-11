recipe-version: 1

# Recipe: `tool_authn` — A1, refuses anonymous

**This is an engine check, tier `readonly`.** ITest runs it itself, from the
manifest's `mcp_tool` point and the server's live tool list. There is no test
file to write, nothing for sync to stub, and nothing that can go stale. It never
calls a write, destructive or unknown tool, and it sends only sentinel
arguments. It still touches the server — an anonymous `tools/list`, and one
anonymous `tools/call` of a read tool — so it runs only where the readonly tier
runs, and never against a host the declaration did not name.

The check lives in `itest/checks/authority.py` (`check_a1`); `docs/checks.md` is
the library's reference. This file is the policy: what A1 proves, what it does
not, and what a reviewer does with each answer.

## 1. What the check proves

A1 asks one question of every declared tool: **does the server turn away a
caller who brings no credential?**

It always probes anonymously. Over HTTP no `Authorization` header is sent; over
stdio the server is launched with the credential variable *removed* from its
environment, so an ambient token in your shell cannot make an anonymous probe
look authenticated.

What it does depends on the tool's mutation class — the stricter of the class
the manifest records and the class the live listing gives now, so a stale or
edited manifest can never talk A1 into calling a destructive tool:

| class | what A1 does |
|---|---|
| `read`, `informational` | **Calls it once**, anonymously, with sentinel arguments (§3). |
| `write`, `destructive` | **Never calls it.** Judges it from the anonymous session: was an anonymous caller admitted to a session that lists this tool? |
| `unknown` | **Never calls it** — it may be either. Reported `not_verifiable`. |

Once per server per run it also takes an anonymous `tools/list` and records the
outcome on every A1 result as evidence. An anonymous listing is common (many
servers publish their catalogue) and is **not** a failure by itself.

## 2. What the check does not prove

- **Not that an authenticated caller is admitted.** A1 is the refusal half. The
  happy path is the server's own tests' job, or a future generated check.
- **Not tenant isolation.** A caller with *a* credential reading another
  tenant's data is A2, a generated check that needs a second identity.
- **Not per-tool authorization on a mutating tool.** A write tool is never
  called, so a server that admits anonymous sessions but checks the caller
  *inside* each mutating tool reads as `critical` here. That is deliberate:
  authentication belongs before the tool, and a destructive tool reachable by an
  anonymous session is the finding even if the tool body happens to hold. The
  evidence says `basis: anonymous-session` so a reviewer knows which it was.
- **Not what a tool error means.** A read tool that answers the sentinel with a
  tool error was reached anonymously, but "no such record" and "not for you" look
  the same from outside. A1 says `not_verifiable` rather than guess.

## 3. Sentinel arguments

Built from the tool's input schema, required parameters only:

| parameter type | value sent |
|---|---|
| string | the declaration's `sentinels.nonexistent_id` |
| integer, number | `0` |
| boolean | `false` (which is also what refuses a `confirm`-style gate) |
| anything else | nothing — A1 reports `not_verifiable` and names the parameter |

Nothing optional is ever sent. The sentinel comes from
`.itest/tools/<server>.yaml` at the project root; a tool that needs a string
sentinel on a server with no declaration there is `not_verifiable`, never called
with a guessed value.

## 4. Statuses, and what each means for a reviewer

| status | means |
|---|---|
| `pass` | The anonymous call was refused (read tools), or the anonymous session was refused before any tool (mutating tools). The guard held. |
| `fail` | A read or informational tool **answered** an anonymous caller. Anyone who can reach the server can use it. |
| `critical` | An anonymous session was admitted and **lists a write or destructive tool**. Nothing authenticates a caller before it. Stop and escalate, exactly as `http_probe`'s unauthenticated-unsafe-2xx. |
| `not_verifiable` | The listing or the call failed in transport, the tool answered with a tool error, the tool is hidden from the anonymous listing (hidden is not refused), its class is unknown, or no sentinel could be built. The detail says which. It is never a pass. |

A1 never returns `changed`: whether a server refuses anonymous callers is a fact
about now, not a drift from a recording.

## 5. Evidence fields

| field | meaning |
|---|---|
| `server`, `tool` | The point. |
| `mutation` | The class A1 acted on — the stricter of `recorded_mutation` and `live_mutation`. |
| `recorded_mutation`, `live_mutation` | What the manifest says; what the live listing says now (`null` if no listing could be read). |
| `called` | Whether a `tools/call` was sent. Always `false` for write, destructive and unknown tools. |
| `arguments` | The sentinel arguments sent, or `null`. |
| `basis` | `call` (the verdict rests on a call) or `anonymous-session` (on the anonymous listing). |
| `call_status` | The probe's own status for the call, when one was made. |
| `anonymous_listing` | `{status: admitted \| refused \| error, detail, tool_count, lists_tool}` — the once-per-server anonymous `tools/list`. |

Every string is scrubbed: the credential's value never appears, even if the
server echoes it.

## 6. Standards mapping

- **OWASP Top 10 for Agentic Applications:** ASI03, Identity and Privilege
  Abuse — an agent-reachable capability with no identity in front of it.
- **OWASP Top 10 for LLM Applications:** LLM06, Excessive Agency, for the
  `critical` case (an unauthenticated path to a mutating capability).
- **Semgrep MCP security cheatsheet:** server tab, row 4 (authorization).
- **AgBOM (OWASP Agent Control Standard):** no attribute mapped yet; the
  anonymous-listing evidence is the natural input to one.

## 7. How to generate the test

**Nothing to generate.** A1 is an engine check: it runs from the manifest during
`itest verify`, and no per-tool file exists or should be written. To see whether
it applies to a tool:

```sh
itest traits --for reference-mcp/delete_record
```

(A1's `applies_when` is `always`.) `itest traits` ships with the engine-check
wiring in sync and verify; until it is on your install, the answer is the A1 row
of `itest/traits/traits.yaml`. If a stub for A1 ever appears in a test file, it
predates the engine check; do not implement it by hand.

## 8. What the reviewer does with a failure

- **`critical`** — treat as an incident on the server, not a test to adjust. An
  anonymous caller can reach a tool that writes or destroys. Put authentication
  in front of the transport (the MCP spec's authorization is transport-level),
  then re-run. Never relax the check, and never declare the tool `read` to route
  around it — the live class wins over the manifest's anyway.
- **`fail`** — decide whether the tool is *meant* to be public. If it is, that
  is a person's claim: write it in the declaration's per-tool `notes`, and expect
  A1 to keep saying `fail` — a public read is still a read anyone can make. If it
  is not meant to be public, fix the server's auth.
- **`not_verifiable`** — read the detail. A transport error is an unreachable
  server (the same finding plan reports). A tool error means the call got in; look
  at the server's logs for whether the tool itself refused. A missing sentinel
  means the declaration is absent from the project root. None of these is a pass.
- **`pass`** with `anonymous_listing.status: admitted` — the tool refused, but
  the catalogue is public. That is common and often fine; the report shows it so
  the choice is visible.

## 9. Reference server

`examples/reference-mcp/` exercises every branch: its guarded mount (`/mcp`)
refuses anonymous sessions (every tool `pass`), and its deliberately **open
mount** (`/open/mcp`) admits them — `get_guide` is a `fail`, `fetch_record`'s 404
tool error is `not_verifiable`, and `delete_record` is `critical`.
`tests/test_checks.py` pins each outcome, and proves by spying on `call_tool`
that no write, destructive or unknown tool is ever called.
