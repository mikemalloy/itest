# The trait table: `itest/traits/traits.yaml`

A declared MCP tool gets a set of **checks**, one per applicable **trait**.
Which traits apply is data, not code. The engine holds no copy of the rules. It
reads [`itest/traits/traits.yaml`](../itest/traits/traits.yaml), which ships in
the wheel and is loaded through `importlib.resources`, and evaluates each row
against each tool's recorded attributes.

The governing principle:

> The amount of generated code a human must maintain is proportional to the
> facts only a human could supply, never to the number of tools.

## The format

```yaml
version: 1

families:
  authority: Authority
  blast: Blast radius
  containment: Containment
  change: Change

traits:
  - id: blast.destructive_gating   # <family>.<slug>; unique; the identity everywhere
    code: BLAST-2                  # a display label for narrow columns; unique
    family: blast                  # must be one of `families`, and the id's prefix
    name: destructive gating
    applies_when: mutation == destructive
    tier: active                   # static | readonly | active (the environment policy's tiers)
    kind: generated                # engine | generated
    recipe: tool_gating.md         # the skill file that says how the check works
    standards: [ASI02, LLM03]      # the published ids the trait answers; never invented
```

| column | meaning |
|---|---|
| `id` | The trait's identity: `<family>.<slug>`. It appears in the manifest, the ledger, docstrings, and — with the dot written `__` where a name cannot hold one — in binding and fixture names (`test_delete_record__blast__destructive_gating`, `blast__destructive_gating_fixtures`). |
| `code` | A short display label (`AUTH-1`, `BLAST-2`, `CONTAIN-3`, `CHANGE-1`) for narrow table columns. Never an identity, never a dispatch key. |
| `family` | Groups traits on the readiness page. Must be one of the table's `families`, and the first half of the id. |
| `applies_when` | When the trait applies to a tool (grammar below). |
| `tier` | What the check runs as, and what the environment policy gates on. Active-tier checks live in their own file. |
| `kind` | Who runs the check (see below). |
| `recipe` | The file in the bundled skill (`itest-implementer/references/recipes/`) that describes the check. |
| `standards` | The published ids the trait answers (below). Verified against the published lists, never invented; an unknown prefix refuses the table. |

### Ids, codes and standards

The shipped traits, and what each answers:

| id | code | standards |
|---|---|---|
| `authority.anonymous` | AUTH-1 | ASI03, LLM02, semgrep-server-4, CWE-306 |
| `authority.tenant_isolation` | AUTH-2 | ASI03, LLM02, semgrep-server-18, CWE-639 |
| `authority.backing_least_privilege` | AUTH-3 | ASI03, LLM03, CWE-269 |
| `authority.delegation` | AUTH-4 | ASI03, semgrep-server-8, CWE-441 |
| `blast.mutation_class` | BLAST-1 | ASI02, LLM03, ACS-AgBOM |
| `blast.destructive_gating` | BLAST-2 | ASI02, LLM03 |
| `blast.egress` | BLAST-3 | ASI04, LLM02, semgrep-server-21 |
| `blast.audit` | BLAST-4 | ACS-AgBOM, CWE-778 |
| `containment.parameter_scope` | CONTAIN-1 | ASI02, semgrep-server-19, CWE-639 |
| `containment.expression_passthrough` | CONTAIN-2 | ASI05, semgrep-server-20, semgrep-server-22, CWE-77, CWE-89 |
| `containment.output_hygiene` | CONTAIN-3 | LLM02, LLM10, CWE-209 |
| `change.inventory` | CHANGE-1 | ASI04, LLM04, semgrep-client-1, ACS-AgBOM |
| `change.schema_drift` | CHANGE-2 | ASI04, LLM04, ACS-AgBOM |
| `change.description_drift` | CHANGE-3 | ASI04, ASI01, LLM01, semgrep-client-2 |

The ids were verified on 2026-09-11 against the published 2026 lists. A
standards entry is one of:

- `ASI01`–`ASI10` — OWASP Top 10 for Agentic Applications (December 2025):
  ASI01 Agent Goal Hijack, ASI02 Tool Misuse, ASI03 Identity & Privilege
  Abuse, ASI04 Agentic Supply Chain, ASI05 Unexpected Code Execution, and
  ASI06–ASI10, which ITest does not cover (`itest standards` says so).
- `LLM01`–`LLM10` — OWASP Top 10 for LLM Applications, 2026 edition: LLM01
  Prompt Injection, LLM02 Sensitive Information Disclosure, LLM03 Excessive
  Agency (LLM03 in 2026, not LLM06), LLM04 Supply Chain, LLM10 Improper Output
  Handling.
- `semgrep-server-<row>` / `semgrep-client-<row>` — the Semgrep MCP security
  cheatsheet, by tab and row.
- `CWE-<n>` — a Common Weakness Enumeration entry.
- `ACS-AgBOM` — the Agent Bill of Materials from the OWASP Agent Control
  Standard v0.1.

Anything else refuses the table, naming the value; an empty list stays legal.
The ids ride into verify's ledger on every check (`tools.servers[].tools[]
.checks[].standards`), into the readiness page (the first id beside each
trait's slug, the full list in the cell's detail) and into
`itest traits` / `itest traits --for`.

**Why `change.description_drift` maps to ASI04 *and* ASI01 *and* LLM01.** It
is the counter-intuitive row and the strongest thing this table says. A tool's
description is the text the model reads when deciding whether to call that
tool. So an unreviewed change to it is a behaviour change shipped without
review — supply chain, ASI04 and LLM04's neighbourhood — and a *malicious* one
is an injection aimed squarely at the agent's tool choice: a prompt injection
(LLM01) whose target is the agent's goal (ASI01, Agent Goal Hijack). The hash
comparison is the same either way; what it defends is not one thing.

**Old ids.** The first tables used AN-style ids (`A1`, `B2`, …). They read as
OWASP ids to a security reader and were ours, so they were renamed; the map is
`itest/traits/ids.py`, and it is a rename, not a trait rule. Anything written
with an old id still loads, mapped to the new one on read: a manifest
(`traits_planned`, each test's `trait`, and the engine cases' names, since the
engine module names no trait itself), a verify ledger read by `itest report
--from`, a declaration's `traits:` list, and a binding calling
`run_generated_check("B2", ...)`. The next sync — a no-op one included — writes
the manifest back with the new ids. A binding's function name is a function on
disk, so it keeps the name it was generated with.

### `kind`: engine or generated

- **`engine`**: the engine runs the check straight from the manifest. No
  per-tool file exists. Each declared server gets at most two engine modules
  (`itest_tests/tools_<server>/test_<server>__engine.py`, plus `…_active.py`).
  The module holds a single test, parametrized at collection time over every
  (tool, engine trait) the manifest records, which calls
  `itest.checks.run_engine_check`. Add a tool and it is picked up without
  regenerating anything. The engine records each `CheckResult` for verify's
  ledger. A `changed` or `not_verifiable` result is recorded and then skipped
  rather than failed: it is a finding that waits on a human (AT RISK), not a
  failing check (BLOCKED).
- **`generated`**: the check needs a fact only a human can supply, such as a
  second tenant's record or the identity the server should act as. Sync writes
  one **thin binding** per (tool, trait) into
  `test_<server>__generated[_active].py`. A binding is a frozen docstring
  (`itest point: <id>  trait: <T>  schema: <hash>`), one import from
  `itest.checks` and one call. The facts it needs come from a `<trait>_fixtures`
  fixture in the server's `conftest.py`, which is **yours**. Sync writes it
  once, with a skipping placeholder per generated trait, and never touches it
  again.

### The loader refuses

The loader rejects the whole table, naming the trait and the bad value, when it
finds any of these:

- a duplicate id, or an id that is not `<family>.<slug>` in lower case (an old
  `A1`-style id included) or does not start with its own family
- a duplicate code, or one that is not upper-case letters, a hyphen and a
  number
- a `standards` entry outside the known prefixes (`ASI`, `LLM`,
  `semgrep-server-`, `semgrep-client-`, `CWE-`, `ACS-`), named in the error —
  an empty list is fine
- an unknown family
- a `kind` other than `engine` or `generated`
- a `tier` the environment policy does not define
- an `applies_when` that does not parse, or that names an unknown attribute

A typo must never quietly delete a check.

### The table hash

`trait_table_hash()` is a 12-character sha256 of the table's *parsed* content.
Each `applies_when` is re-rendered canonically before hashing. Re-indenting,
adding comments or respacing an expression leaves the hash alone; changing what
any row says moves it. The manifest records the hash so a sync can report that
the table changed.

## The `applies_when` grammar

```
always                        every tool
<field>                       the field is truthy
<field> present               the field is not null
<field> == <value>            equality (none, true, false, integers and bare words are literals)
<field> != <value>            inequality
<field> in [<a>, <b>]         membership
<A> and <B> and ...           every clause holds
<A> or <B>                    either side holds
```

`and` binds tighter than `or`, so `a and b or c` reads as `(a and b) or c`.
There are no parentheses and no `not`: the grammar is deliberately the
smallest that expresses the rows that ship, and a rule that needs more is a
rule worth arguing about in a review. Every clause is evaluated — nothing
short-circuits — so an unknown attribute anywhere in an expression is an
error, never a clause that happened not to be reached.

Fields: `mutation`, `egress`, `approval`, `active`, `has_free_form_input`,
`auth.second_tenant_env`, `audit.sink`, `identity.runs_as`, `transport.kind`
(`stdio` | `http`) and `auth.enforced_over_stdio` (a boolean, default false).
Each one comes from the tool's point in the manifest; the last five are facts
about the server, copied onto each of its points at plan time.

The one row that uses `or` is `authority.anonymous`:

```
applies_when: transport.kind == http or auth.enforced_over_stdio
```

Over stdio the process boundary *is* the authentication boundary — whoever can
spawn the subprocess is authorised — so there is no anonymous caller to refuse
unless the server checks a credential of its own. A plain stdio server gets
**no** anonymous check: not a passing one, not a failing one, not a
`not_verifiable` one. `itest traits --for <server>/<tool>` prints
`does not apply` with that rule beside it, so the absence is explained rather
than looking forgotten. [docs/checks.md](checks.md) says why in full.

A per-tool `traits:` list in the declaration replaces the table for that tool.
`[none-of-these]` withholds every check, and it requires `notes`. `active: false`
withholds that tool's active-tier checks however they were chosen.

## What happens on sync

Every `itest plan` evaluates the **current** table against **every** declared
tool's current attributes. It compares the result with that tool's
`traits_planned`, the set the last sync recorded. A manifest from before
`traits_planned` existed falls back to the per-trait tests it already has.

```
Trait changes (2):
  trait table changed (5458224b3c52 → 9c1d0e7a2b44): 1 tools gained checks, 1 tools retired checks.
  +authority.tenant_isolation on reference-mcp/get_guide (rule: always)
  −blast.destructive_gating on reference-mcp/delete_record (mutation changed)
```

- **A generated trait that newly applies**: sync appends a binding, unless the
  tool already has a test for that trait.
- **An engine trait that newly applies**: nothing is written. `traits_planned`
  is updated and the engine module runs the new case.
- **A trait that no longer applies**: its test is **retired**. The file stays
  on disk and the entry stays in the manifest (`retired: true`), but verify
  never runs it and reports it as `not_applicable`. If the trait applies again,
  the same entry comes back under the same id. Nothing generated is ever
  deleted.
- **A per-tool stub for an engine trait** — what sync wrote for every trait
  before the engine module existed — is retired in place the same way, even
  though its trait still applies: the engine module is the only thing that runs
  that trait, and the old stub would only ever run as a skip beside it. A
  no-op sync retires one too, and it is never restored.
- **A tool's mutation class** is a drift attribute, along with `schema_hash`,
  `description_hash` and `annotations_hash`. An annotation flip is `changed`
  (`mutation: read → destructive (annotation)`), and the traits it moves are
  trait changes.
- A plan with any trait change, or with a table hash not yet recorded, is not
  a no-op.
- An unreachable server's points are held as last recorded and are not
  recomputed.

## Ownership

A generated file whose content still matches its ownership hash belongs to
ITest and may be regenerated. A file a human has edited is frozen: ITest
reports it and never rewrites it. `conftest.py` is human-owned from birth and
has no ownership hash.

A binding records the schema it was generated against (`schema:` in its
docstring). When the tool's schema moves, every sync — a no-op one included —
regenerates the bindings ITest still owns against the new schema and records
their new hash, so after a sync they are `current` again. A verify that runs
before that sync reports them `stale`, never `current`: nothing has read them
against the tool as it now is. A hand-edited binding is never regenerated.

## Lifecycle states

Every check in verify's tool ledger carries a `state`:

| state | meaning | counts toward VERIFIED |
|---|---|---|
| `current` | ITest-owned, and generated against the tool's current schema | yes |
| `hand_edited` | a human changed the file (its ownership hash differs) | yes |
| `stale` | generated against a schema the tool no longer has: hand-edited (frozen until a human re-reads it), or ITest-owned and not yet regenerated (the next `itest sync` regenerates it) | **no**, and a server with one cannot be VERIFIED |
| `recipe_newer` | the recipe moved since the check was generated | not emitted yet: nothing records a recipe version |
| `not_applicable` | retired: the trait no longer applies | no |
| `orphan` | the tool is gone, and the test is kept | no (listed under exceptions) |

VERIFIED is a coverage claim. A tool counts as verified only when every trait
in its `traits_planned` has a counted check that passed. The readiness page
tags each non-current cell with its state and adds a line under the tool
summary, for example `reference-mcp needs attention: 3 hand-edited, 1 stale`.

## Commands

```
itest traits                            # the table, headed by its hash: slug, code, family,
                                        #   kind, tier, applies_when, standards, recipe
itest traits --for <server>/<tool>      # every trait decided for one tool: APPLIES or does
                                        #   not apply, the rule that decided, the standards
itest traits --json                     # either of the above as JSON
itest recipes [--recipes-dir DIR]       # every recipe the table names: path, present/missing, traits
itest recipes --json
itest standards --from verify.json      # this run's checks through the published standards
itest standards --json                  # the rollup as JSON (reads stdin when --from is omitted)
```

## The standards view

A security reader wants to see the report through the framework they already
report against. The families stay the skeleton — a clean partition that carries
the tier semantics and never gets renumbered when OWASP publishes an edition —
and a trait maps to *several* ids, so standards cannot be the structure. They
are a **second lens** over the same checks:

- **In the ledger.** `itest verify --output json` emits `tools.standards[]`
  beside `tools.servers[]`: one entry per published id any check in the run
  cites — `id`, `title`, `framework`, `coverage`, `note`, `checks`, and
  `statuses` (pass / fail / critical / changed / not_verifiable /
  not_applicable / held_out / not_run). It is derived at emit time from the
  checks themselves, never from a hand-maintained second list, so the two
  cannot drift.
- **What is not covered, named.** `itest/traits/standards.yaml` ships the full
  OWASP Agentic list (ASI01–ASI10) and the LLM list (LLM01–LLM10) with their
  titles. Every ASI entry appears in the rollup: cited ones carry their
  counts; uncited ones read `not_covered` with a one-line reason
  (model-behaviour risk, multi-agent transport out of scope, …). ASI05 is
  `partial` — `containment.expression_passthrough` probes the boundary; static
  analysis finds the code path — and ASI01 stays `not_covered` even though
  `change.description_drift` cites it, because goal hijack is a
  model-behaviour risk and ITest asserts boundaries, not judgment. Coverage is
  a statement about *this run's evidence*: a covered id nothing in the run
  cites reads `not_covered` too. LLM ids appear only where a check cites them;
  that list is a neighbouring framework, not ITest's coverage claim.
- **On the page.** A "Standards" band sits under the tool posture tiles: one
  row per ASI entry, the status counts as a compact bar, and covered / partial
  / not covered stated plainly. Uncovered rows are present and quiet — a scope
  statement, not a finding. The families grid beside it is unchanged.
- **In the terminal.** `itest standards` prints the same rollup from a
  `verify --output json` document (`--from`, or stdin), with `--json` for the
  list exactly as the ledger carries it. It reads only the ledger and the
  shipped catalogue; no server is contacted.

Both commands read only the manifest and the table; no server is contacted.
`--for` decides from the attributes the manifest recorded. It says so when the
table has changed since the last sync, and it exits 1 on an unknown server or
tool, listing the ones that exist. `itest recipes` searches `skills/`,
`.claude/skills/` and `~/.claude/skills/` for
`itest-implementer/references/recipes`. A missing recipe file is reported as
missing, not raised as an error.
