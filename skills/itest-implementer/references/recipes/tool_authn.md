recipe-version: 1

# Recipe: `tool_authn` — A1, refuses anonymous

**This is an engine check, tier `readonly`.** ITest runs it itself, from the
manifest's `mcp_tool` point and the server's live tool list. There is no test
file to write, nothing for sync to stub, and nothing that can go stale. It never
calls a write, destructive or unknown tool, and it sends only sentinel
arguments. It still touches the server — one anonymous session per server, and
one anonymous `tools/call` of a read tool when that session is admitted — so it
runs only where the readonly tier runs, and never against a host the declaration
did not name.

The check lives in `itest/checks/authority.py` (`check_a1`); `docs/checks.md` is
the library's reference. This file is the policy: what A1 proves, what it does
not, and what a reviewer does with each answer.

## 1. What the check proves

A1 asks one question of every declared tool: **does the server turn away a
caller who brings no credential?** It judges only what it observed.

It always probes anonymously. Over HTTP no `Authorization` header is sent; over
stdio the server is launched with the credential variable *removed* from its
environment, so an ambient token in your shell cannot make an anonymous probe
look authenticated.

**Step 1 — the front door, once per server.** An anonymous `initialize` +
`tools/list`. If the server refuses the anonymous session, **every tool on it
passes**: "server refuses anonymous sessions; per-tool call not attempted". This
is the common good case — authentication in front of the transport, where the
MCP spec puts it — and no tool is called at all.

**Step 2 — behind an open front door, per tool.** If the anonymous session is
admitted, what A1 does depends on the tool's mutation class: the stricter of the
class the manifest records and the class the live listing gives now, so a stale
or edited manifest can never talk A1 into calling a mutating tool.

| class | what A1 does |
|---|---|
| `read`, `informational` | **Calls it once**, anonymously, with sentinel arguments (§3). Refused — by the transport or inside the tool — is a pass; answered is a fail. |
| `write`, `destructive` | **Does not call it.** `not_verifiable`: the tool's own guard can only be proven by an active-tier call on a non-production environment. |
| `unknown` | **Does not call it** — it may be either. `not_verifiable`. |

## 2. What the check does not prove

- **Not whether a mutating tool guards itself behind an open front door.** A
  readonly check cannot call a write tool, and a listing is not a call. So a
  write or destructive tool on a server that admits anonymous sessions is
  `not_verifiable` — never `critical`, which is reserved for a *demonstrated*
  admission. Proving it takes **A1 active**: an anonymous call with sentinel
  arguments, active tier only, non-production only, `critical` on admission. It
  is not built.
- **Not that an authenticated caller is admitted.** A1 is the refusal half. The
  happy path is the server's own tests' job, or a future generated check.
- **Not tenant isolation.** A caller with *a* credential reading another
  tenant's data is A2, a generated check that needs a second identity.
- **Not, with certainty, what a tool error means.** When an anonymous call
  reaches a read tool and it answers with a tool error, A1 reads the error's
  text. An **auth-shaped** error — it contains `unauthorized`,
  `unauthenticated`, `forbidden`, `permission`, `not allowed`, `401` or `403`,
  case-insensitive, with the tool's own name removed — is the tool refusing the
  caller: `pass`, "refused inside the tool: <quoted error>", evidence `basis:
  tool-level refusal`. **Any other** tool error (not found, validation,
  internal) means the anonymous caller reached the tool's logic: `fail`, quoting
  the error. This is a **heuristic**: a server that words its refusal
  differently reads as a `fail`, and one that says "forbidden" about something
  other than the caller reads as a `pass`. The detail always quotes the error so
  a reviewer can overrule it.

## 3. Sentinel arguments

Built from the tool's input schema in the anonymous listing, required
parameters only:

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
| `pass` | The server refused the anonymous session (front door); or it admitted the session and refused the anonymous call on this read tool — at the transport, or inside the tool with an auth-shaped error ("refused inside the tool: …"). The guard held. |
| `fail` | A read or informational tool **answered** an anonymous caller — "anonymous call succeeded on a read tool" — or answered with a tool error that is not auth-shaped, so its logic ran for an anonymous caller. Anyone who can reach the server can read through it: data exposure, not mutation. |
| `not_verifiable` | The anonymous session could not be attempted; or the session was admitted and the tool mutates (deferred to A1 active), its class is unknown, it is hidden from the anonymous listing, no sentinel could be built, or the call failed in transport. The detail says which. It is never a pass. |

A1 never returns `critical` — that needs a demonstrated admission on a mutating
tool, which is A1 active's to show — and never `changed`: whether a server
refuses anonymous callers is a fact about now, not a drift from a recording.

## 5. Evidence fields

| field | meaning |
|---|---|
| `server`, `tool` | The point. |
| `anonymous_listing` | The front door: `refused`, `admitted`, or `error`. Recorded on every result. |
| `anonymous_listing_detail` | What the server said (e.g. the HTTP 401), or how many tools it listed anonymously. |
| `anonymous_tool_count` | Tools listed to the anonymous caller, when admitted. |
| `mutation` | The class A1 acted on — the stricter of `recorded_mutation` and `live_mutation`. |
| `recorded_mutation`, `live_mutation` | What the manifest says; what the anonymous listing says now (`null` when it was not read). |
| `called` | Whether a `tools/call` was sent. Always `false` for write, destructive and unknown tools, and for every tool behind a refused front door. |
| `arguments` | The sentinel arguments sent, or `null`. |
| `call_status` | The probe's own status for the call, when one was made. |
| `basis` | `tool-level refusal` when the pass rests on an auth-shaped tool error rather than a transport refusal; absent otherwise. |

Every string is scrubbed: the credential's value never appears, even if the
server echoes it.

## 6. Standards mapping

- **OWASP Top 10 for Agentic Applications:** ASI03, Identity and Privilege
  Abuse — an agent-reachable capability with no identity in front of it.
- **OWASP Top 10 for LLM Applications:** LLM02, Sensitive Information
  Disclosure, for a `fail` (an anonymous read that answers).
- **Semgrep MCP security cheatsheet:** server tab, row 4 (authorization) — the
  front door.
- **AgBOM (OWASP Agent Control Standard):** no attribute mapped yet; the
  front-door evidence is the natural input to one.

## 7. How to generate the test

**Nothing to generate.** A1 is an engine check: it runs from the manifest during
`itest verify`, and no per-tool file exists or should be written. To see whether
it applies to a tool:

```sh
itest traits --for reference-mcp/delete_record
```

(A1's `applies_when` is `always`.) `itest traits` reads the tool's attributes
from the manifest, so run it after `itest sync`; the rule itself is the A1 row
of `itest/traits/traits.yaml`. If a stub for A1 ever appears in a test file, it
predates the engine check; do not implement it by hand.

## 8. What the reviewer does with a failure

- **`fail`** — an anonymous caller read through the tool. Decide whether it is
  *meant* to be public. If it is not, put authentication in front of the
  transport, so the front door refuses and every tool passes. If it is, that is a
  person's claim: write it in the declaration's per-tool `notes`, and expect A1
  to keep saying `fail` — a public read is still a read anyone can make. A `fail`
  quoting a tool error means the tool's logic ran for the anonymous caller. If
  the quoted error is really the tool refusing in words the heuristic does not
  know, the tool is guarded — but fix the transport anyway, so the front door
  refuses and nothing rests on the wording of an error.
- **`pass` with `basis: tool-level refusal`** — read the quoted error. The tool
  turned the caller away, but only inside its own code, behind an open front
  door; every other tool on that server has to get that right too.
- **`not_verifiable` on a mutating tool** ("anonymous session admitted; this tool
  mutates…") — the server lets anonymous callers in, and nothing readonly can show
  whether this tool stops them. Treat the open front door as the finding: close
  it and the tool passes at the door. Until then, the question belongs to A1
  active on a non-production environment. Never declare the tool `read` to get it
  called — the live class wins over the manifest's anyway.
- **other `not_verifiable`** — read the detail. An error at the front door is an
  unreachable server (the same finding plan reports). A missing sentinel means
  the declaration is absent from the project root. A tool hidden from the
  anonymous listing was not called. None of these is a pass.

## 9. Reference server

`examples/reference-mcp/` exercises every branch: its guarded mount (`/mcp`)
refuses anonymous sessions, so every tool passes at the front door and nothing
is called. Its deliberately **open mount** (`/open/mcp`) admits them:
`get_guide` and `fetch_record` (whose 404 tool error is not auth-shaped, so the
tool ran for the anonymous caller) are `fail`, and `delete_record`, `create_record` and `update_record` are
`not_verifiable`, deferred to A1 active. `tests/test_checks.py` pins each
outcome, proves A1 never says `critical`, and proves by spying on `call_tool`
that no write, destructive or unknown tool is ever called.
