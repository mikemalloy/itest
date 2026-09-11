recipe-version: 1

# Recipe: `tool_mutation_class` — B1, mutation class

**This is an engine check, tier `readonly`, and it never calls the tool.** ITest
runs it itself from the manifest's `mcp_tool` point and the server's live tool
list. There is no test file to write and nothing that can go stale.

The check lives in `itest/checks/blast_radius.py` (`check_b1`); `docs/checks.md`
is the library's reference. This file is the policy.

## 1. What the check proves

The mutation class — `read`, `write`, `destructive`, `informational` — is the
most consequential fact about a tool: it decides whether a probe may call it at
all, and which other traits apply. ITest never lets a developer simply state it
(see `docs/declarations.md`). B1 is the standing check that the statements about
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

**B1 agreement cannot catch a tool that lies consistently.** Every input above
is a *statement*. A tool whose annotation and name agree with each other, and are
both false, looks perfectly consistent — and B1 **passes** it.

`lookalike_read` in `examples/reference-mcp/` is exactly that tool: annotated
`readOnlyHint=true`, named like a read, and it increments a counter on every
call. B1 passes it, by design, and `tests/test_checks.py` pins that pass. An
annotation lie is visible only by **observing behaviour**: calling the tool with a
sentinel and looking at what changed afterwards.

That is **B1 observed**, the next recipe in this family: active tier, and it
needs a read tool named in the declaration to look at the store with. It is not
built. `lookalike_read` is its fixture. Until it ships, a B1 `pass` means "the
statements agree", never "the tool does what it says".

## 3. Statuses, and what each means for a reviewer

| status | means |
|---|---|
| `pass` | Every statement that exists agrees. |
| `changed` | The live class differs from the manifest's: the class moved since the last sync (a server started annotating, or changed an annotation). Sync turns this into drift; the reviewer re-approves the tool at its new class. |
| `fail` | The annotation and the name **disagree**, and no declaration settles it: `annotation says read-only, name says create_*; declare it`. One of the two is wrong and ITest cannot know which. |
| `not_verifiable` | Nothing classifies the tool (no annotation, the name suggests nothing); or only the declaration states a class, with nothing to agree with; or the tool is not listed (D1 reports that); or no listing could be taken. |

A declaration **settles** a disagreement only by agreeing with the annotation
(provenance `confirmed`). A declaration that sides with the name is refused at
plan time, before B1 could ever see it.

## 4. Evidence fields

| field | meaning |
|---|---|
| `server`, `tool` | The point. |
| `manifest` | `{mutation, mutation_source}` as recorded. |
| `live` | `{class, source}` from `classify_mutation` of the live listing; `source` is `annotation`, `name`, `conflict:name-says-<x>` or `unknown`. |
| `annotations` | Exactly the annotation fields the server sent (`{}` when none). |
| `name_class` | What the name alone says, or `null`. |
| `declared` | The class the declaration stated, or `null`. |

## 5. Standards mapping

- **OWASP Top 10 for Agentic Applications:** ASI02, Tool Misuse and
  Exploitation — a tool whose declared effect does not match its real one is how
  an agent is led into a write it was told was a read.
- **OWASP Top 10 for LLM Applications:** LLM06, Excessive Agency.
- **OWASP Agent Control Standard, AgBOM:** the tool's **mutation** attribute. B1
  is what keeps that attribute honest against the server's own statements.
- **Semgrep MCP security cheatsheet:** no row mapped. The cheatsheet asks what a
  server does with client data; it has no row for cross-checking annotations.

## 6. How to generate the test

**Nothing to generate.** B1 is an engine check: it runs from the manifest during
`itest verify`. To see whether it applies to a tool:

```sh
itest traits --for reference-mcp/lookalike_read
```

(B1's `applies_when` is `always`.) `itest traits` ships with the engine-check
wiring in sync and verify; until it is on your install, the answer is the B1 row
of `itest/traits/traits.yaml`.

The **observed** variant — next, active tier, needs a read tool named in the
declaration — will be a generated check following
[`tool_recipe_shape.md`](tool_recipe_shape.md). It is not claimed here.

## 7. What the reviewer does with a failure

- **`fail` (annotation and name disagree)** — find out which is true, from the
  server's code or its owner. Then either fix the server (the annotation or the
  name), or write the true class in the declaration under `tools.<name>.mutation`.
  If the truth agrees with the annotation, the declaration settles it and B1
  passes; if it agrees with the name, `itest plan` will refuse — correctly,
  because the server is then describing itself falsely and must be fixed.
- **`changed`** — the server's class moved. Re-run `itest plan`: the changeset
  shows the tool as changed. A move towards `write` or `destructive` changes which
  traits apply (B2, B4, and A1's handling), so review it as a new tool.
- **`not_verifiable` (unknown)** — the server says nothing about the tool. Ask
  its owner to annotate it; meanwhile a declared class records a person's belief,
  and B1 will say plainly that nothing corroborates it.
- **`pass` on a tool you suspect** — B1 cannot help (§2). Note it for B1 observed.
