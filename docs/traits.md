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
  A: Authority
  B: Blast radius
  C: Containment
  D: Change

traits:
  - id: B2                         # a family letter and a number; unique
    family: B                      # must be one of `families`
    name: destructive gating
    applies_when: mutation == destructive
    tier: active                   # static | readonly | active (the environment policy's tiers)
    kind: generated                # engine | generated
    recipe: tool_gating.md         # the skill file that says how the check works
```

| column | meaning |
|---|---|
| `id` | The trait's address. It appears in stub names, docstrings, the manifest and the ledger. |
| `family` | Groups traits on the readiness page. Must be one of the table's `families`. |
| `applies_when` | When the trait applies to a tool (grammar below). |
| `tier` | What the check runs as, and what the environment policy gates on. Active-tier checks live in their own file. |
| `kind` | Who runs the check (see below). |
| `recipe` | The file in the bundled skill (`itest-implementer/references/recipes/`) that describes the check. |

### `kind`: engine or generated

- **`engine`**: the engine runs the check straight from the manifest. No
  per-tool file exists. Each declared server gets one engine module per tier
  (`itest_tests/tools_<server>/test_<server>__engine.py`, plus `…_active.py`).
  The module holds a single test, parametrized at collection time over every
  (tool, engine trait) the manifest records, which calls
  `itest.checks.run_engine_check`. Add a tool and it is picked up without
  regenerating anything.
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

- a duplicate id
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
<A> and <B> and ...           every clause holds (there is no `or`)
```

Fields: `mutation`, `egress`, `approval`, `active`, `has_free_form_input`,
`auth.second_tenant_env`, `audit.sink`, `identity.runs_as`. Each one comes from
the tool's point in the manifest; the last three are facts about the server.

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
  +A2 on reference-mcp/get_guide (rule: always)
  −B2 on reference-mcp/delete_record (mutation changed)
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

## Lifecycle states

Every check in verify's tool ledger carries a `state`:

| state | meaning | counts toward VERIFIED |
|---|---|---|
| `current` | ITest-owned, and generated against the tool's current schema | yes |
| `hand_edited` | a human changed the file (its ownership hash differs) | yes |
| `stale` | hand-edited, *and* generated against a schema the tool no longer has | **no**, and a server with one cannot be VERIFIED |
| `recipe_newer` | the recipe moved since the check was generated | not emitted yet: nothing records a recipe version |
| `not_applicable` | retired: the trait no longer applies | no |
| `orphan` | the tool is gone, and the test is kept | no (listed under exceptions) |

VERIFIED is a coverage claim. A tool counts as verified only when every trait
in its `traits_planned` has a counted check that passed. The readiness page
tags each non-current cell with its state and adds a line under the tool
summary, for example `reference-mcp needs attention: 3 hand-edited, 1 stale`.

## Commands

```
itest traits                            # the table, headed by its hash
itest traits --for <server>/<tool>      # every trait decided for one tool, and why
itest traits --json                     # either of the above as JSON
itest recipes [--recipes-dir DIR]       # every recipe the table names: path, present/missing, traits
itest recipes --json
```

Both commands read only the manifest and the table; no server is contacted.
`--for` decides from the attributes the manifest recorded. It says so when the
table has changed since the last sync, and it exits 1 on an unknown server or
tool, listing the ones that exist. `itest recipes` searches `skills/`,
`.claude/skills/` and `~/.claude/skills/` for
`itest-implementer/references/recipes`. A missing recipe file is reported as
missing, not raised as an error.
