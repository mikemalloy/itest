# Evidence sources: `.itest/sources/<name>.yaml`

ITest asserts boundaries and produces facts: for every declared tool, checks
that say whether the tool *would let* a caller do something. An **evidence
source** adds a second, different kind of evidence to the same page — a
red-team tool's results, read from a file, joined onto the tools ITest already
inventories, and shown as a dated **rate** beside the boundary checks. Red
says how often an agent could be talked into calling a tool. Blue says whether
the tool would let it. The pair sits on one row; neither replaces the other.

**The rule, in one sentence: judgment evidence is not a check.** It has no
lifecycle state, never counts toward VERIFIED, never changes the verdict,
never appears in coverage, and its standards ids never enter the standards
rollup's covered count. A passing red-team run can never turn a cell green.
`tests/test_evidence_never_counts.py` proves it: the same project with and
without a source yields identical VERIFIED counts, verdict and rollup.

ITest never runs the red-team tool, never shells out, never calls a model.
The reader is file I/O and arithmetic: offline, deterministic, seconds.

## The file

One file per source. The file name is the source's name.

```yaml
# .itest/sources/promptfoo-lab.yaml
kind: promptfoo                     # the only kind this build reads
server: reference-mcp               # a server declared under .itest/tools/
results: evidence/promptfoo.json    # relative to the project directory; OR
# results_env: PROMPTFOO_RESULTS    # the NAME of a variable holding the path (CI)
agent: "sonnet-5 + haiku-4-5 via agent.js"   # free text, shown on the source line
standards: [ASI01, LLM01]           # ids this evidence bears on — shown as
                                    # external evidence, never as coverage
max_age_days: 7                     # older than this at sync time → stale (default 7)
```

Exactly one of `results` / `results_env`. Unknown keys are errors. Standards
ids are validated with the same rule the trait table uses (ASI, LLM,
semgrep-server-/semgrep-client-, CWE-, ACS-). A results path is not a secret
and may be written literally; nothing here names or reads a credential.

## What sync does with it

`itest sync` reads every source **last**, after all its other work, so
nothing upstream can depend on it, and prints one line per source:

```
evidence promptfoo-lab (promptfoo): run eval-zyk-2026-09-23T18:26:29 at 2026-09-23T18:26:29.565Z, 12 rows, 2 tools matched, 0 unmatched
```

It is best-effort and never blocking. A missing or unreadable results file is
recorded as `unreadable` with the reason; a file naming a server the manifest
does not inventory is `server_not_declared`; tools the manifest does not list
are `unmatched` on the source line. Sync exits 0 in every case and the page
shows what happened. Staleness — `(sync time − run time) > max_age_days` —
is computed at sync, stored, and never recomputed at report time: the report
shows what sync knew.

The manifest gains two top-level lists, `evidence` (one record per matched
tool) and `sources` (one line per file), written only when a source exists.
Neither sits under `tests`, because neither is a test. No schema bump.

## The two join shapes (promptfoo 0.123)

A results row is `$.results.results[i]`. The reader handles both shapes and
records which it found; a results file never has to say which kind it is.

| target | where the tool is | one call is | refused / succeeded |
| --- | --- | --- | --- |
| **agent** (a provider driving an agent over the server's tools) | every tool called is in `row.metadata.toolCalls[]`, each `{id, name, input, output, is_error}` | one entry | `is_error` |
| **direct** (promptfoo's `mcp` provider, one tool per test) | `row.metadata.toolName`, singular, plus `toolArgs` and `originalPayload` | the row | `row.success` stands in |

Run identity is `$.evalId`; run time `$.results.timestamp`; tool version
`$.metadata.promptfooVersion`. Rows that name no tool contribute to the
denominator and to the source line, never to a tool row. A tool that was never
called is **absent** — the reader says so by absence, never by inventing a
zero row. The join key is `(server, tool name)` and nothing else.

**Targeting is never inferred from prompt text.** It is recorded only when a
row's `testCase.metadata.target_tool` names a tool; without it the lane reads
"targeting not declared by the harness".

**Two readings of one tool.** The run-wide counts (`rows_with_call`, `calls`,
`refused`, `succeeded`) see every row and count call entries, which is what
the detail lane shows as its rate. The targeted reading counts **rows, and
only targeted rows**: of the rows that targeted the tool,
`targeted_rows_with_call` called it at all; of those, `targeted_rows_refused`
had at least one refused call to it and `targeted_rows_succeeded` at least
one that went through — a row refused twice is one row, and a row refused
and then admitted counts in both. A tool called on a row the harness aimed
elsewhere raises `rows_with_call` and nothing else: that is a benign call,
not an induced one. All three are `None` when the tool was never targeted.
The Answer's red-team line reads only the targeted counts, so it cannot say
"2 induced; 3 refused".

## Reading a row with both lanes

The rate is shown as its parts — `calls N · refused R · of T rows` — never as
a percentage alone, because a denominator of 12 is the honest context. Read a
tool's row as four cases:

| red (evidence lane) | blue (boundary checks) | reading |
| --- | --- | --- |
| **inducible** — the agent called it | **ungated** — the checks say the tool would let it | the finding: a talked-into call that the server would honour |
| **inducible** | **contained** — a check refused or gated it | the guard held under pressure; keep the guard |
| **not inducible** — targeted, never called | **ungated** | the model held; the boundary is still the thing to fix |
| **not targeted** — no harness declared it, or the lane is absent | either | the run says nothing about this tool; absence is not a zero |

`examples/reference-mcp` with the shipped fixture is the third row for
`delete_record` in spirit — every prompt names it and no agent called it — but
the harness declared no `target_tool`, so the page honestly shows no lane for
it at all.

## On the page

Under each tool row that has evidence, a second lane labelled *external
evidence* with the source's standards ids, the rate, the run id and time, the
agent, and *stale* when sync said so. The source lines sit in the tools
section. In the standards view, an id only a source cites appears under a
separate *external evidence* heading with no covered count; an id a check
also cites keeps the check's coverage, with the source listed beside it.

The lane's words are *rate*, *run*, *stale*, *external evidence*. Never
*pass*, *fail*, *verified* or *covered* — those belong to the boundary lane.

## Not yet built

Targeting declared by the harness end to end (a promptfoo config that writes
`target_tool`), the `garak` kind, and evidence via promptfoo's own MCP server.
See the scope ledger in [DESIGN.md](../DESIGN.md).
