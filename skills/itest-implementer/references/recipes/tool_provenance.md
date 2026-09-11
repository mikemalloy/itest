recipe-version: 1

# Recipe: `tool_provenance` — D1 inventory, D2 schema drift, D3 description drift

**These are engine checks, tier `readonly`, and none of them calls a tool.**
ITest runs them from the manifest's `mcp_tool` points and **one** live
`tools/list` per server per verify run. There is no test file to write and
nothing that can go stale.

The checks live in `itest/checks/change.py` (`check_d1`, `check_d2`,
`check_d3`); `docs/checks.md` is the library's reference. This file is the
policy for all three: they are one question asked three ways — *is this still the
tool somebody reviewed?*

## 1. What the checks prove

| check | question | compares |
|---|---|---|
| **D1 inventory** | Does the server still list this tool — and does it list tools nobody reviewed? | the point's tool name against the live listing; the manifest's tools for the server against the live listing |
| **D2 schema drift** | Does the tool still accept what it accepted when reviewed? | the point's `schema_hash` against the live one (canonical JSON, so key order cannot move it) |
| **D3 description drift** | Does the tool still *say* what it said when reviewed? | the point's `description_hash` against the live one |

D3 matters more than it looks. A description is text a model reads and acts on,
so rewording one changes the agent's instructions even when the schema is
untouched — the shape of a tool-poisoning "rug pull". A server that changes what
a tool says without changing what it accepts moves exactly one hash, which is why
D2 and D3 are separate.

D1's other half is **undeclared** tools: live tools with no manifest point. They
are callable by an agent and were never reviewed. The list is computed once per
server and carried on every D1 result for that server, so any single row shows
it.

## 2. What the checks do not prove

- **Not that a listed tool is safe** — only that it is the one reviewed.
- **Not what changed in words.** The manifest keeps hashes, not text, so D3 can
  say *that* a description changed and give both hashes, not show the previous
  wording. `itest plan` shows the live text; the reviewer compares it with what
  they approved.
- **Not the output schema.** Only the input schema is hashed today.
- **Not a tool hidden from this caller.** D1 lists with the credential when the
  run is authenticated and anonymously otherwise; a tool the server shows only to
  some callers can read as an orphan to others. The evidence says which listing
  was used.

## 3. Statuses, and what each means for a reviewer

| check | status | means |
|---|---|---|
| D1 | `pass` | The tool is listed. |
| D1 | `fail` | `not in tools/list; orphan` — withdrawn, renamed, or hidden from this caller. |
| D2 / D3 | `pass` | The hash matches the recording. |
| D2 / D3 | `changed` | The hash moved since the last sync; both hashes are in the evidence. Sync turns this into drift. |
| all | `not_verifiable` | No listing could be taken (unreachable; an anonymous listing refused — the detail names the credential variable that would unlock it; or an authenticated listing whose credential will not resolve); or, for D2/D3, the tool is not listed (D1 reports it) or the manifest recorded no hash. Never a pass. |

With no credential resolving — the normal case for a stdio server — the checks
run on the **anonymous** listing when the server admits one; the evidence says
`listing: anonymous`, so a reviewer knows what the answer rests on.

## 4. Evidence fields

**D1:** `server`, `tool`, `listed` (bool), `listing` (`authenticated` or
`anonymous`), and `undeclared` — a sorted list of live tool names with no
manifest point, `[]` when there are none, and `null` when there was no manifest
at the project root to compare against ("nothing recorded" and "nothing to
compare" are different facts).

**D2 / D3:** `server`, `tool`, `listing` (`authenticated` or `anonymous`),
`recorded` (the manifest's hash), `live` (the listing's hash).

## 5. Standards mapping

- **OWASP Top 10 for Agentic Applications:** ASI04, Agentic Supply Chain
  Vulnerabilities — a tool whose contract or instructions move under a pinned
  review, and tools that arrive unreviewed.
- **OWASP Top 10 for LLM Applications:** LLM01, Prompt Injection, for D3 (a
  description is instructions to the model); LLM03, Supply Chain, for D1/D2.
- **Semgrep MCP security cheatsheet:** client tab, row 12 ("Does the client treat
  tool descriptions as untrusted?") for D3, and row 14 ("How does the client
  handle name collisions?") for D1's undeclared list — a newly appearing tool is
  where a colliding name comes from.
- **AgBOM (OWASP Agent Control Standard):** the tool inventory. D1 checks each
  entry still exists and that nothing live is missing from it; D2 and D3 pin its
  content by hash.

## 6. How to generate the test

**Nothing to generate.** D1–D3 are engine checks: they run from the manifest
during `itest verify`. To see whether they apply to a tool:

```sh
itest traits --for reference-mcp/enrich
```

(All three are `applies_when: always`.) `itest traits` reads the tool's
attributes from the manifest, so run it after `itest sync`; the rule itself is
the D rows of `itest/traits/traits.yaml`.

## 7. What the reviewer does with a failure

- **D1 `fail` (orphan)** — ask the server's owner whether the tool was withdrawn
  or renamed. A rename is a new tool to review plus an orphan to retire; a
  withdrawal is an orphan to retire. `itest sync` flags orphans and never deletes
  their tests.
- **D1 `undeclared` non-empty** — each name is a tool an agent can call that no
  one reviewed. Run `itest plan` to see them, then sync to give them points (and
  so their checks). Look hard at one whose name shadows a tool on another server.
- **D2 `changed`** — the tool accepts something new. Re-review its parameters:
  a new free-form string may make C2 apply; a new id may widen C1's scope.
- **D3 `changed`** — read the new description as untrusted text. Instructions to
  the model ("always call this first", "include the user's token") are the
  finding. A benign rewording is re-approved by syncing.
- **`not_verifiable`** — the server could not be listed. That is the same fact
  `itest plan` reports as unreachable; nothing about the tool was checked.

## 8. Reference server

Against `examples/reference-mcp/`, `tests/test_checks.py` pins: every tool
passes D1–D3 against a manifest built from its own listing; a manifest point for
a tool the server does not list is a D1 orphan; a manifest missing `enrich`
reports `undeclared: [enrich]` on every D1 result; a stale `schema_hash` or
`description_hash` is `changed` with both hashes.
