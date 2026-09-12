recipe-version: 1

# Recipe: `tool_mutation_class` — `blast.mutation_class`, mutation class

**This is an engine check, tier `readonly`, and it never calls the tool.** ITest
runs it itself from the manifest's `mcp_tool` point and the server's live tool
list. There is no test file to write and nothing that can go stale.

The check lives in `itest/checks/blast_radius.py` (`check_blast__mutation_class`); `docs/checks.md`
is the library's reference. This file is the policy.

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

That is **mutation class observed**, the next recipe in this family: active tier, and it
needs a read tool named in the declaration to look at the store with. It is not
built. `lookalike_read` is its fixture. Until it ships, a `blast.mutation_class` `pass` means "the
statements agree", never "the tool does what it says".

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

The **observed** variant — next, active tier, needs a read tool named in the
declaration — will be a generated check following
[`tool_recipe_shape.md`](tool_recipe_shape.md). It is not claimed here.

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
- **`pass` on a tool you suspect** — `blast.mutation_class` cannot help (§2). Note it for mutation class observed.
