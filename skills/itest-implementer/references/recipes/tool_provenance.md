recipe-version: 1

# Recipe: `tool_provenance` — `change.inventory` inventory, `change.schema_drift` schema drift, `change.description_drift` description drift

**These are engine checks, tier `readonly`, and none of them calls a tool.**
ITest runs them from the manifest's `mcp_tool` points and **one** live
`tools/list` per server per verify run. There is no test file to write and
nothing that can go stale.

The checks live in `itest/checks/change.py` (`check_change__inventory`, `check_change__schema_drift`,
`check_change__description_drift`); `docs/checks.md` is the library's reference. This file is the
policy for all three: they are one question asked three ways — *is this still the
tool somebody reviewed?*

## 1. What the checks prove

| check | question | compares |
|---|---|---|
| **`change.inventory` inventory** | Does the server still list this tool — and does it list tools nobody reviewed? | the point's tool name against the live listing; the manifest's tools for the server against the live listing |
| **`change.schema_drift` schema drift** | Does the tool still accept what it accepted when reviewed? | the point's `schema_hash` against the live one (canonical JSON, so key order cannot move it) |
| **`change.description_drift` description drift** | Does the tool still *say* what it said when reviewed? | the point's `description_hash` against the live one |

`change.description_drift` matters more than it looks. A description is text a model reads and acts on,
so rewording one changes the agent's instructions even when the schema is
untouched — the shape of a tool-poisoning "rug pull". A server that changes what
a tool says without changing what it accepts moves exactly one hash, which is why
`change.schema_drift` and `change.description_drift` are separate.

`change.inventory`'s other half is **undeclared** tools: live tools with no manifest point. They
are callable by an agent and were never reviewed. The list is computed once per
server and carried on every `change.inventory` result for that server, so any single row shows
it.

## 2. What the checks do not prove

- **Not that a listed tool is safe** — only that it is the one reviewed.
- **Not what changed in words.** The manifest keeps hashes, not text, so `change.description_drift` can
  say *that* a description changed and give both hashes, not show the previous
  wording. `itest plan` shows the live text; the reviewer compares it with what
  they approved.
- **Not the output schema.** Only the input schema is hashed today.
- **Not a tool hidden from this caller.** `change.inventory` lists with the credential when the
  run is authenticated and anonymously otherwise; a tool the server shows only to
  some callers can read as an orphan to others. The evidence says which listing
  was used.

## 3. Statuses, and what each means for a reviewer

| check | status | means |
|---|---|---|
| `change.inventory` | `pass` | The tool is listed. |
| `change.inventory` | `fail` | `not in tools/list; orphan` — withdrawn, renamed, or hidden from this caller. |
| `change.schema_drift` / `change.description_drift` | `pass` | The hash matches the recording. |
| `change.schema_drift` / `change.description_drift` | `changed` | The hash moved since the last sync; both hashes are in the evidence. Sync turns this into drift. |
| all | `not_verifiable` | No listing could be taken (unreachable; an anonymous listing refused — the detail names the credential variable that would unlock it; or an authenticated listing whose credential will not resolve); or, for `change.schema_drift`/`change.description_drift`, the tool is not listed (`change.inventory` reports it) or the manifest recorded no hash. Never a pass. |

With no credential resolving — the normal case for a stdio server — the checks
run on the **anonymous** listing when the server admits one; the evidence says
`listing: anonymous`, so a reviewer knows what the answer rests on.

## 4. Evidence fields

**`change.inventory`:** `server`, `tool`, `listed` (bool), `listing` (`authenticated` or
`anonymous`), and `undeclared` — a sorted list of live tool names with no
manifest point, `[]` when there are none, and `null` when there was no manifest
at the project root to compare against ("nothing recorded" and "nothing to
compare" are different facts).

**`change.schema_drift` / `change.description_drift`:** `server`, `tool`, `listing` (`authenticated` or `anonymous`),
`recorded` (the manifest's hash), `live` (the listing's hash).

## 5. Standards mapping

- **OWASP Top 10 for Agentic Applications:** ASI04, Agentic Supply Chain
  Vulnerabilities — a tool whose contract or instructions move under a pinned
  review, and tools that arrive unreviewed.
- **OWASP Top 10 for LLM Applications:** LLM01, Prompt Injection, for `change.description_drift` (a
  description is instructions to the model); LLM03, Supply Chain, for `change.inventory` / `change.schema_drift`.
- **Semgrep MCP security cheatsheet:** client tab, row 12 ("Does the client treat
  tool descriptions as untrusted?") for `change.description_drift`, and row 14 ("How does the client
  handle name collisions?") for `change.inventory`'s undeclared list — a newly appearing tool is
  where a colliding name comes from.
- **AgBOM (OWASP Agent Control Standard):** the tool inventory. `change.inventory` checks each
  entry still exists and that nothing live is missing from it; `change.schema_drift` and `change.description_drift` pin its
  content by hash.

## 6. How to generate the test

**Nothing to generate.** `change.inventory`, `change.schema_drift` and `change.description_drift` are engine checks: they run from the manifest
during `itest verify`. To see whether they apply to a tool:

```sh
itest traits --for reference-mcp/enrich
```

(All three are `applies_when: always`.) `itest traits` reads the tool's
attributes from the manifest, so run it after `itest sync`; the rule itself is
the D rows of `itest/traits/traits.yaml`.

## 7. What the reviewer does with a failure

- **`change.inventory` `fail` (orphan)** — ask the server's owner whether the tool was withdrawn
  or renamed. A rename is a new tool to review plus an orphan to retire; a
  withdrawal is an orphan to retire. `itest sync` flags orphans and never deletes
  their tests.
- **`change.inventory` `undeclared` non-empty** — each name is a tool an agent can call that no
  one reviewed. Run `itest plan` to see them, then sync to give them points (and
  so their checks). Look hard at one whose name shadows a tool on another server.
- **`change.schema_drift` `changed`** — the tool accepts something new. Re-review its parameters:
  a new free-form string may make `containment.expression_passthrough` apply; a new id may widen `containment.parameter_scope`'s scope.
- **`change.description_drift` `changed`** — read the new description as untrusted text. Instructions to
  the model ("always call this first", "include the user's token") are the
  finding. A benign rewording is re-approved by syncing.
- **`not_verifiable`** — the server could not be listed. That is the same fact
  `itest plan` reports as unreachable; nothing about the tool was checked.

## 8. Reference server

Against `examples/reference-mcp/`, `tests/test_checks.py` pins: every tool
passes `change.inventory`, `change.schema_drift` and `change.description_drift` against a manifest built from its own listing; a manifest point for
a tool the server does not list is a `change.inventory` orphan; a manifest missing `enrich`
reports `undeclared: [enrich]` on every `change.inventory` result; a stale `schema_hash` or
`description_hash` is `changed` with both hashes.
