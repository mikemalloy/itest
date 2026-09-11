# Checks: `itest/checks`

A declaration (see [`declarations.md`](declarations.md)) turns an MCP server into
`mcp_tool` points, and the trait table (`itest/traits/traits.yaml`) decides which
traits each tool gets. `itest/checks` is the library that **runs one trait's
check for one tool**.

Two kinds of check share it:

- **Engine checks.** ITest runs them itself, from the manifest's point and the
  server's live tool list. No per-tool file exists, so nothing can go stale.
  Every engine check is readonly by definition. Most traits are engine checks.
- **Generated checks.** For the few traits that need a fact only a person can
  supply — a second tenant's record, how to read the audit sink — sync writes a
  one-line binding that calls this same library with a human-owned fixture. The
  shape is fixed in
  [`tool_recipe_shape.md`](../skills/itest-implementer/references/recipes/tool_recipe_shape.md).

## The contract

```python
from itest.checks import CheckResult, run_engine_check, run_generated_check

@dataclass(frozen=True)
class CheckResult:
    status: str          # "pass" | "fail" | "critical" | "changed" | "not_verifiable"
    detail: str          # one line, human-readable, never a credential
    evidence: dict | None = None   # JSON-ready; what the report may show

def run_engine_check(trait_id: str, point: dict, target: McpTarget,
                     *, authenticated: bool) -> CheckResult
def run_generated_check(trait_id: str, point: dict, target: McpTarget,
                        *, fixtures: dict) -> CheckResult
```

`point` is the manifest's `mcp_tool` point as a plain dict: `id`, `type`,
`server`, `target` (the tool name), and `attributes` (`mutation`,
`mutation_source`, `egress`, `approval`, `active`, `schema_hash`,
`description_hash`, `annotations`). `source` is accepted in place of `server`, so a
raw manifest entry works too.

`authenticated` chooses how the **reference listing** — the `tools/list` a check
compares the manifest against — is taken: with the target's credential, or
anonymously. An authenticated listing whose credential is unset is
`not_verifiable`, never quietly anonymous; a target that names no credential has
nothing to authenticate with and is listed as the server allows. A1 always probes
anonymously whatever this says.

The statuses:

| status | meaning |
|---|---|
| `pass` | The check ran and the property holds. |
| `fail` | The check ran and the property does not hold. |
| `critical` | A **demonstrated** anonymous admission on a write or destructive tool — never an inference from a listing. Stop and escalate. No readonly engine check produces it; it is reserved for A1 active. |
| `changed` | The live server differs from what the last sync recorded. Sync makes this drift. |
| `not_verifiable` | The check could not run; the detail says why. **Never a pass.** |

Neither entry point raises for anything a server did or for a target it cannot
use (no url, a refused private host, an unreadable `.itest/.env`): each is
`not_verifiable` with the reason, because one unusable server must not stop the
rest of a verify run.

## The registry

`ENGINE_CHECKS` maps a trait id to its check; each check module registers with
`@engine_check("<id>")` when the package is imported. `run_engine_check` dispatches
on it, and an id with no entry returns `not_verifiable` with detail
`no engine check for <id>`.

`GENERATED_CHECKS` is **empty**. `run_generated_check` answers every id with
`not_verifiable` (`no generated check for <id> yet`). It exists now so the thin
bindings sync writes import and run cleanly before the A2/B2/B4 checks behind them
ship; a binding moves from "awaiting check" to live by upgrading ITest, with no
project change.

Each check is one function, `check_<id>(point, target, *, authenticated)`, whose
docstring says what it does, what each status means, and which standards rows it
answers — written to be printed by a future `itest explain`.

## The engine checks

| id | module | what it calls | statuses | standards |
|---|---|---|---|---|
| **A1** refuses anonymous | `authority.py` | anonymous `initialize` + `tools/list` once per server (the front door); behind an admitted session, one anonymous `tools/call` of a **read or informational** tool with sentinel arguments. Never calls a write, destructive or unknown tool. | `pass` (front door refused, or the read call refused — at the transport or by an auth-shaped tool error), `fail` (a read tool answered, or ran and returned any other tool error), `not_verifiable` (mutating tool behind an open door, unknown, hidden, no sentinel, transport). Never `critical`. | OWASP Agentic ASI03; LLM02; Semgrep server tab row 4 |
| **B1** mutation class (agreement) | `blast_radius.py` | the reference listing only | `pass` (all statements agree), `changed` (live class ≠ manifest), `fail` (annotation and name disagree, unsettled), `not_verifiable` (unknown, declaration only, not listed) | OWASP Agentic ASI02; LLM06; AgBOM mutation attribute |
| **D1** inventory | `change.py` | the reference listing; the project manifest | `pass`, `fail` (`not in tools/list; orphan`), `not_verifiable`; `evidence.undeclared` on every result | OWASP Agentic ASI04; Semgrep client tab row 14 |
| **D2** schema drift | `change.py` | the reference listing | `pass`, `changed` (both hashes), `not_verifiable` | OWASP Agentic ASI04; Semgrep client tab rows 12, 14 |
| **D3** description drift | `change.py` | the reference listing | `pass`, `changed` (both hashes), `not_verifiable` | OWASP Agentic ASI04; LLM01; Semgrep client tab row 12 |

The recipes explain each result for a reviewer:
[`tool_authn.md`](../skills/itest-implementer/references/recipes/tool_authn.md),
[`tool_mutation_class.md`](../skills/itest-implementer/references/recipes/tool_mutation_class.md),
[`tool_provenance.md`](../skills/itest-implementer/references/recipes/tool_provenance.md).

### A1 judges only what it observed

A1 starts at the **front door**: one anonymous `initialize` + `tools/list` per
server. A server that refuses anonymous sessions passes A1 for every tool, with
"server refuses anonymous sessions; per-tool call not attempted" — the common
good case, and no tool is called. Behind an admitted session, a read or
informational tool gets one anonymous call with sentinel arguments (refused →
`pass`, answered → `fail`). A tool error is read by what it says: an
**auth-shaped** error — containing `unauthorized`, `unauthenticated`,
`forbidden`, `permission`, `not allowed`, `401` or `403`, case-insensitive, with
the tool's own name removed — is a refusal inside the tool (`pass`, "refused
inside the tool: <quoted error>", evidence `basis: tool-level refusal`); any
other tool error (not found, validation, internal) means the anonymous caller
reached the tool's logic (`fail`, quoting it). The vocabulary is one tuple,
`authority.AUTH_REFUSAL_MARKERS`, and it is a heuristic: the quoted error is
always in the detail so a reviewer can overrule it.

A write or destructive tool is **not called** and is `not_verifiable`:
"anonymous session admitted; this tool mutates, so its own guard can only be
proven by an active-tier call on a non-production environment". The class used
is the **stricter** of the manifest's and the live listing's, so a stale or
edited manifest cannot talk A1 into calling a mutating tool; an `unknown` tool is
not called either.

A1 never returns `critical`. That status means a *demonstrated* anonymous
admission on a mutating tool, and only a call can demonstrate one — which is
**A1 active** (active tier, non-production only, sentinel arguments), not this
check. A listing is never evidence enough.

## Sentinel rules

A1 is the only engine check that calls a tool, and it sends only values that
cannot address anything real:

- required parameters only — nothing optional;
- a string gets the declaration's `sentinels.nonexistent_id`, read from
  `.itest/tools/<server>.yaml` at the project root (schema-validated; the
  environment-policy cross-check is not needed to read a sentinel);
- an integer or number gets `0`, a boolean `false`;
- any other required type has no safe value, and the check is `not_verifiable`
  naming the parameter;
- a tool needing a string sentinel on a server with no declaration at the
  project root is `not_verifiable`, never called with a guessed value.

Refuse-by-default stays: no check passes `allow_mutating`, and
`tests/test_checks.py` proves it by spying on every `call_tool` across every engine
check, every reference tool and all three mounts.

## The scrub rule

Every string in a `CheckResult` — the detail, and every key and value in the
evidence — passes through two existing scrubbers before it leaves the library:
the target credential's own value is replaced with `***` (the MCP probe's rule,
applied again here because a server can echo a token into an anonymous call's
error, where the probe had no credential to scrub), then
`itest.core.redact.text_scrubber` removes credential-shaped text and
pseudonymizes account ids. A value under a key ending `_hash` skips the second
pass only, so a twelve-digit drift hash is never mistaken for an account id. The
registry wrapper scrubs whichever way a check is reached, and `run_engine_check`
scrubs again.

## The per-run listing cache

Within one verify run a server is listed **once per mode** (authenticated,
anonymous), however many tools and traits ask — and a listing that failed is
cached as a failure, so a dead server is asked once. The manifest's tool names
per server and each server's declaration are cached the same way. Call
`itest.checks.clear_cache()` at the start of each run; the cache is keyed by the
target's fields (an `McpTarget` holds a list and is not itself hashable).

Checks read the project root from the working directory, as `itest verify` does:
`.itest/manifest.yaml` for D1's undeclared list (absent → `undeclared: null`,
never `[]`), `.itest/tools/<server>.yaml` for A1's sentinel, and `.itest/.env`
for a credential by name.

## The pinned limit of B1

B1 compares **statements** — the manifest, the annotation, the name, the
declaration. A tool whose annotation and name agree and are both false looks
consistent, and B1 passes it. `lookalike_read` in `examples/reference-mcp/` is
annotated read-only, named like a read, and mutates; B1 passes it, **by design**,
and `tests/test_checks.py` pins that pass. An annotation lie is visible only by
observing behaviour, which is **B1 observed** — active tier, a sentinel call
followed by a look at the store through a read tool named in the declaration. It
is not built; `lookalike_read` is its fixture.

## Not in this library yet

- A1 active (active tier, non-production only): an anonymous call on each
  mutating tool behind an open front door, with sentinel arguments — `critical`
  on admission. This is where the MCP probe's proven critical path belongs.
- B1 observed (active); A2, A3, A4, B2, B4 generated checks; B3, C1, C2 checks.
- `itest explain <trait>` to print a check's docstring.
- Wiring into sync, verify and the report — the tool ledger's statuses come from
  here, but the wiring is its own change.
