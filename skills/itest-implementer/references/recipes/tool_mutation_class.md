recipe-version: 2

# Recipe: `tool_mutation_class` — `blast.mutation_class` and `blast.mutation_class_observed`, mutation class

Two engine checks share this recipe. **`blast.mutation_class` (BLAST-1) is
readonly tier and never calls the tool**: it compares statements. **`blast.mutation_class_observed`
(BLAST-1b) is active tier and calls the tool once**: it watches behaviour. ITest
runs both itself from the manifest's `mcp_tool` point and the server's live
tool list. There is no test file to write and nothing that can go stale.

Both live in `itest/checks/blast_radius.py` (`check_blast__mutation_class`,
`check_blast__mutation_class_observed`); `docs/checks.md` is the library's
reference. This file is the policy.

## 1. What the check proves

The mutation class — `read`, `write`, `destructive`, `informational` — is the
most consequential fact about a tool: it decides whether a probe may call it at
all, and which other traits apply. ITest never lets a developer simply state it
(see `docs/declarations.md`). `blast.mutation_class` is the standing check that the statements about
it still **agree**:

1. the **manifest** — `mutation` and `mutation_source` as the last sync recorded
   them;
2. the **server's annotation**, read fresh from the live listing
   (`readOnlyHint`, `destructiveHint`);
3. the **name heuristic** on its own (`delete_*`, `create_*`, `get_*`, …);
4. the **declaration**, when it stated a class — which the manifest records as
   provenance `confirmed` (it agreed with detection) or `declared` (detection
   found nothing and the declaration filled the gap).

A pass names the sources that agreed:
`destructive: annotation destructiveHint; name delete_*; declared destructive`.

## 2. What the check does not prove — the pinned limit

**`blast.mutation_class` agreement cannot catch a tool that lies consistently.** Every input above
is a *statement*. A tool whose annotation and name agree with each other, and are
both false, looks perfectly consistent — and `blast.mutation_class` **passes** it.

`lookalike_read` in `examples/reference-mcp/` is exactly that tool: annotated
`readOnlyHint=true`, named like a read, and it increments a counter on every
call. `blast.mutation_class` passes it, by design, and `tests/test_checks.py` pins that pass. An
annotation lie is visible only by **observing behaviour**: calling the tool with a
sentinel and looking at what changed afterwards.

That is **`blast.mutation_class_observed`** (§8 below): active tier, and it
needs a read tool named in the declaration to look at the store with.
`lookalike_read` is its fixture and it catches it. A `blast.mutation_class`
`pass` on its own still means "the statements agree", never "the tool does
what it says"; the observed row is what says the second thing.

## 3. Statuses, and what each means for a reviewer

| status | means |
|---|---|
| `pass` | Every statement that exists agrees. |
| `changed` | The live class differs from the manifest's: the class moved since the last sync (a server started annotating, or changed an annotation). Sync turns this into drift; the reviewer re-approves the tool at its new class. |
| `fail` | The annotation and the name **disagree**, and no declaration settles it: `annotation says read-only, name says create_*; declare it`. One of the two is wrong and ITest cannot know which. |
| `not_verifiable` | Nothing classifies the tool (no annotation, the name suggests nothing); or only the declaration states a class, with nothing to agree with; or the tool is not listed (`change.inventory` reports that); or no listing could be taken. |

A declaration **settles** a disagreement only by agreeing with the annotation
(provenance `confirmed`). A declaration that sides with the name is refused at
plan time, before `blast.mutation_class` could ever see it.

## 4. Evidence fields

| field | meaning |
|---|---|
| `server`, `tool` | The point. |
| `listing` | `authenticated` or `anonymous` — which listing the live class was read from. With no credential resolving, `blast.mutation_class` runs on the anonymous listing when the server admits one; a refused one is `not_verifiable`, naming the variable that would unlock it. |
| `manifest` | `{mutation, mutation_source}` as recorded. |
| `live` | `{class, source}` from `classify_mutation` of the live listing; `source` is `annotation`, `name`, `conflict:name-says-<x>` or `unknown`. |
| `annotations` | Exactly the annotation fields the server sent (`{}` when none). |
| `name_class` | What the name alone says, or `null`. |
| `declared` | The class the declaration stated, or `null`. |

## 5. Standards mapping

- **OWASP Top 10 for Agentic Applications:** ASI02, Tool Misuse and
  Exploitation — a tool whose declared effect does not match its real one is how
  an agent is led into a write it was told was a read.
- **OWASP Top 10 for LLM Applications:** LLM03, Excessive Agency (LLM03 in
  the 2026 edition; it was LLM06 in 2025).
- **OWASP Agent Control Standard, AgBOM:** the tool's **mutation** attribute. `blast.mutation_class`
  is what keeps that attribute honest against the server's own statements.
- **Semgrep MCP security cheatsheet:** no row mapped. The cheatsheet asks what a
  server does with client data; it has no row for cross-checking annotations.

## 6. How to generate the test

**Nothing to generate.** `blast.mutation_class` is an engine check: it runs from the manifest during
`itest verify`. To see whether it applies to a tool:

```sh
itest traits --for reference-mcp/lookalike_read
```

(`blast.mutation_class`'s `applies_when` is `always`.) `itest traits` reads the tool's attributes
from the manifest, so run it after `itest sync`; the rule itself is the `blast.mutation_class` row
of `itest/traits/traits.yaml`.

The **observed** variant (§8) is an engine check too: nothing to generate.
It appears in the active engine module for every read or informational tool
of a server whose declaration names `observation.snapshot_tool`.

## 7. What the reviewer does with a failure

- **`fail` (annotation and name disagree)** — find out which is true, from the
  server's code or its owner. Then either fix the server (the annotation or the
  name), or write the true class in the declaration under `tools.<name>.mutation`.
  If the truth agrees with the annotation, the declaration settles it and `blast.mutation_class`
  passes; if it agrees with the name, `itest plan` will refuse — correctly,
  because the server is then describing itself falsely and must be fixed.
- **`changed`** — the server's class moved. Re-run `itest plan`: the changeset
  shows the tool as changed. A move towards `write` or `destructive` changes which
  traits apply (`blast.destructive_gating`, `blast.audit`, and `authority.anonymous`'s handling), so review it as a new tool.
- **`not_verifiable` (unknown)** — the server says nothing about the tool. Ask
  its owner to annotate it; meanwhile a declared class records a person's belief,
  and `blast.mutation_class` will say plainly that nothing corroborates it.
- **`pass` on a tool you suspect** — `blast.mutation_class` cannot help (§2). Declare
  `observation.snapshot_tool` and let `blast.mutation_class_observed` (§8) watch it.

## 8. `blast.mutation_class_observed` — the observed variant

**Active tier. It calls the tool.** For a tool whose resolved class is `read`
or `informational` — the stricter of what the manifest recorded and what the
live listing says now — it first takes a **baseline**: two snapshots through
the declared `observation.snapshot_tool`, in one session, before any call. If
the two differ, the snapshot tool is unstable — timestamps, request ids,
unordered results — and nothing that moves later could be pinned on the
target, so the target is **not called** and the result is `not_verifiable`,
naming the snapshot tool and suggesting a stable projection. Never
`critical`.

Only on a stable baseline does it take three calls in **one session** against
the same state:

1. a snapshot: the snapshot tool, called with sentinel arguments, its result
   normalised (the structured content the server sent, or its text) and
   hashed;
2. the tool under test, once, with sentinel arguments — required parameters
   only, `sentinels.nonexistent_id` for strings, `0` and `false` for the rest;
3. the snapshot again.

A difference means the tool mutated while claiming not to.

### What it proves

That one call of this tool, with arguments that address nothing real, changed
what the snapshot tool shows. That is a demonstrated mutation behind a
read-only claim — the thing no reading of `tools/list` can show.

### What it cannot prove

**One call, one observation window.** A tool that mutates only under other
arguments, only for a record that exists, only sometimes, or in state the
snapshot tool does not show, is not caught, and a `pass` says so in its
detail. A `pass` means "nothing observable moved this once", not "this tool
never writes". The snapshot tool is the reviewer's choice: pick one whose
output reflects the state a lying read tool would touch (`search_records`
reports the store's size, which is why the reference declaration names it).

### Statuses

| status | means |
|---|---|
| `critical` | The snapshots differ. The detail names the tool, its claimed class, what claimed it (`annotation readOnlyHint`, or `name get_*`), the snapshot tool, the sizes, how many paths changed and which (bounded), and both hashes — never a value. An agent has been told this tool is safe to call freely. |
| `pass` | No observable change through the snapshot tool after one sentinel call. |
| `not_verifiable` | No `observation.snapshot_tool` declared (never a pass); the snapshot tool is **unstable** (two baseline snapshots differed, so the target was not called — declare a stable projection), is not listed, is not a read tool, or answered with an error; the tool is not listed or has no sentinel form; the session was refused or failed; or the tool's class is write, destructive or unknown — the check never calls those, and nothing is opened. |

### Where it runs

Active tier: proving a read tool mutates means causing that mutation once, so
it runs only where the committed policy (`.itest/environments.yaml`) allows
`active`, and a production environment refuses it — `held_out` on the page.
The single-session path it uses has **no mutation opt-in at all**: a write or
destructive class anywhere in the sequence refuses the whole sequence before
anything is opened.

### Evidence never carries raw application data

A snapshot view is the server's data: it can hold customer records, tenant
data or PII. A `CheckResult`'s detail flows into verify JSON, the failing-test
traceback and the rendered report, and none of those is reached by the
credential scrubber — it removes tokens and account ids, not a customer's
email. So the two views are **compared in memory and never persisted**. What
a result carries is structure only:

- the before and after **hashes** (over the canonical view);
- the before and after **sizes** (the number of scalar values in the view);
- a **bounded list of changed field names or paths** (`customers[1].email`,
  `total`; at most ten, and `changed_paths_truncated` says when there were
  more, with the full count in `changed_paths_total`).

The detail follows the same rule. `'search_records': 3 -> 4 values, 2
path(s) changed (ids[0], total) (before 50389d988895, after aba90f872549)` is
what it says; the id that was added is not in it, and the tool's own answer
(which a server can echo data into) is not quoted either. A test with a
snapshot tool that returns obviously sensitive content asserts that none of
it appears in the result, the verify JSON or the page. A field *name* is
treated as structure: if your snapshot tool keys a mapping by customer id,
choose a projection that does not.

### Evidence fields

| field | meaning |
|---|---|
| `snapshot_tool` | The declared read tool state was observed through. |
| `claimed`, `claimed_by` | The class the tool claims, and what claimed it. |
| `arguments`, `called`, `call_status` | What the tool was called with, whether it was, and how it answered (`ok`, or `error` for a tool error — it ran either way). |
| `baseline` | `{stable, hashes}`: the two snapshots taken before any call, and whether they agreed. |
| `before`, `after` | `{hash, size}` of each snapshot. Never the view. |
| `changed_paths`, `changed_paths_total`, `changed_paths_truncated` | The changed field names or paths (at most ten), how many there were, and whether the list was cut. |

### What the reviewer does with a `critical`

Stop and escalate. The tool is reachable by an agent under a read-only label,
so every policy that let the agent call reads freely has been letting it
write. Then: fix the server's annotation (`readOnlyHint=false`, and
`destructiveHint` as appropriate) or the tool itself; re-run `itest plan` —
the class moves, the plan says so, and the traits the new class brings
(`blast.destructive_gating`, `blast.audit`, `authority.anonymous`'s handling)
arrive as trait changes. Do not "declare it a write" to silence the check: the
server describing itself falsely is the defect, and a declaration that sides
with the truth against the annotation is refused at plan time anyway.
