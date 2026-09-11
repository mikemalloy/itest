# Checks: `itest/checks`

A declaration (see [`declarations.md`](declarations.md)) turns an MCP server into
`mcp_tool` points, and the trait table (`itest/traits/traits.yaml`) decides which
traits each tool gets. `itest/checks` is the library that **runs one trait's
check for one tool**.

Two kinds of check share it:

- **Engine checks.** ITest runs them itself, from the manifest's point and the
  server's live tool list. An engine check has no per-tool file, so nothing can
  go stale. Most are readonly tier; **`containment.parameter_scope` and `containment.expression_passthrough` are active tier** — they will
  call tools with sentinel injection arguments, which is safe only on a
  non-production environment — and run only where the committed policy
  (`.itest/environments.yaml`) allows `active`. Most traits are engine checks.
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

`authenticated` chooses how the **reference listing** — the `tools/list` `blast.mutation_class` and
`change.inventory`, `change.schema_drift` and `change.description_drift` compare the manifest against — is taken. The runtime passes `True` only
when the server's named credential actually resolves (the shell, then
`.itest/.env`); a name alone is not a credential.

- `authenticated=False` — no credential resolves; reference-mcp over stdio is
  the normal case. The checks run on the **anonymous** listing when the server
  admits one, with `evidence.listing: anonymous`. Only a refused anonymous
  listing is `not_verifiable`, and its detail names the variable that would
  unlock the authenticated one ("export REFERENCE_MCP_TOKEN …" — a name, never
  a value).
- `authenticated=True` — the listing is taken with the credential,
  `evidence.listing: authenticated`. If the credential cannot be resolved after
  all, the result is `not_verifiable`, never quietly anonymous. A target that
  names no credential has nothing to authenticate with and is listed as the
  server allows.

`authority.anonymous` always probes anonymously whatever this says.

The statuses:

| status | meaning |
|---|---|
| `pass` | The check ran and the property holds. |
| `fail` | The check ran and the property does not hold. |
| `critical` | A **demonstrated** anonymous admission on a write or destructive tool — never an inference from a listing. Stop and escalate. No readonly engine check produces it; it is reserved for anonymous-refusal active. |
| `changed` | The live server differs from what the last sync recorded. Sync makes this drift. |
| `not_verifiable` | The check could not run; the detail says why. **Never a pass.** |

Neither entry point raises for anything a server did or for a target it cannot
use (no url, a refused private host, an unreadable `.itest/.env`): each is
`not_verifiable` with the reason, because one unusable server must not stop the
rest of a verify run.

## The registry

`ENGINE_CHECKS` maps a trait's slug (`authority.anonymous`) to its check; each check module registers with
`@engine_check("<id>")` when the package is imported. `run_engine_check` dispatches
on it, and an id with no entry returns `not_verifiable` with detail
`no engine check for <id>`.

`GENERATED_CHECKS` is **empty**. `run_generated_check` answers every id with
`not_verifiable` (`no generated check for <id> yet`). It exists now so the thin
bindings sync writes import and run cleanly before the `authority.tenant_isolation` / `blast.destructive_gating` / `blast.audit` checks behind them
ship; a binding moves from "awaiting check" to live by upgrading ITest, with no
project change.

Both registries are keyed by slug; `run_engine_check` and `run_generated_check`
map an old AN-style id (`A1`) to its slug first, so a binding generated before
the rename still reaches its check.

Each check is one function, `check_<family>__<slug>(point, target, *,
authenticated)` (the slug with its dot written `__`), whose
docstring says what it does, what each status means, and which standards rows it
answers — written to be printed by a future `itest explain`.

## The engine checks

| id | module | what it calls | statuses | standards |
|---|---|---|---|---|
| **`authority.anonymous`** (AUTH-1) refuses anonymous | `authority.py` | anonymous `initialize` + `tools/list` once per server (the front door); behind an admitted session, one anonymous `tools/call` of a **read or informational** tool with sentinel arguments. Never calls a write, destructive or unknown tool. | `pass` (front door refused, or the read call refused — at the transport or by an auth-shaped tool error), `fail` (a read tool answered, or ran and returned any other tool error), `not_verifiable` (mutating tool behind an open door, unknown, hidden, no sentinel, transport). Never `critical`. | OWASP Agentic ASI03; LLM02; Semgrep server tab row 4 |
| **`blast.mutation_class`** (BLAST-1) mutation class (agreement) | `blast_radius.py` | the reference listing only | `pass` (all statements agree), `changed` (live class ≠ manifest), `fail` (annotation and name disagree, unsettled), `not_verifiable` (unknown, declaration only, not listed) | OWASP Agentic ASI02; LLM06; AgBOM mutation attribute |
| **`change.inventory`** (CHANGE-1) inventory | `change.py` | the reference listing; the project manifest | `pass`, `fail` (`not in tools/list; orphan`), `not_verifiable`; `evidence.undeclared` on every result | OWASP Agentic ASI04; Semgrep client tab row 14 |
| **`change.schema_drift`** (CHANGE-2) schema drift | `change.py` | the reference listing | `pass`, `changed` (both hashes), `not_verifiable` | OWASP Agentic ASI04; Semgrep client tab rows 12, 14 |
| **`change.description_drift`** (CHANGE-3) description drift | `change.py` | the reference listing | `pass`, `changed` (both hashes), `not_verifiable` | OWASP Agentic ASI04; LLM01; Semgrep client tab row 12 |

The recipes explain each result for a reviewer:
[`tool_authn.md`](../skills/itest-implementer/references/recipes/tool_authn.md),
[`tool_mutation_class.md`](../skills/itest-implementer/references/recipes/tool_mutation_class.md),
[`tool_provenance.md`](../skills/itest-implementer/references/recipes/tool_provenance.md).

### `authority.anonymous` judges only what it observed

`authority.anonymous` starts at the **front door**: one anonymous `initialize` + `tools/list` per
server. A server that refuses anonymous sessions passes `authority.anonymous` for every tool, with
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
edited manifest cannot talk `authority.anonymous` into calling a mutating tool; an `unknown` tool is
not called either.

`authority.anonymous` never returns `critical`. That status means a *demonstrated* anonymous
admission on a mutating tool, and only a call can demonstrate one — which is
**anonymous-refusal active** (active tier, non-production only, sentinel arguments), not this
check. A listing is never evidence enough.

## Sentinel rules

`authority.anonymous` is the only engine check that calls a tool, and it sends only values that
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
`.itest/manifest.yaml` for `change.inventory`'s undeclared list (absent → `undeclared: null`,
never `[]`), `.itest/tools/<server>.yaml` for `authority.anonymous`'s sentinel, and `.itest/.env`
for a credential by name.

## The pinned limit of `blast.mutation_class`

`blast.mutation_class` compares **statements** — the manifest, the annotation, the name, the
declaration. A tool whose annotation and name agree and are both false looks
consistent, and `blast.mutation_class` passes it. `lookalike_read` in `examples/reference-mcp/` is
annotated read-only, named like a read, and mutates; `blast.mutation_class` passes it, **by design**,
and `tests/test_checks.py` pins that pass. An annotation lie is visible only by
observing behaviour, which is **mutation class observed** — active tier, a sentinel call
followed by a look at the store through a read tool named in the declaration. It
is not built; `lookalike_read` is its fixture.

## Not in this library yet

- anonymous-refusal active (active tier, non-production only): an anonymous call on each
  mutating tool behind an open front door, with sentinel arguments — `critical`
  on admission. This is where the MCP probe's proven critical path belongs.
- mutation class observed (active); `authority.tenant_isolation`, `authority.backing_least_privilege`, `authority.delegation`, `blast.destructive_gating`, `blast.audit` generated checks.
- `itest explain <trait>` to print a check's docstring.
- Engine checks `blast.egress`, `containment.parameter_scope`, `containment.expression_passthrough`, `containment.output_hygiene` (the table names them; `run_engine_check`
  answers `not_verifiable`, "no engine check for <id>").
