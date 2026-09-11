# Declarations: `.itest/tools/<server>.yaml`

A detector reads Terraform and infers integration points. An MCP server is not
in Terraform state, so nothing can infer it — the person who runs it has to say
it exists. That statement is a **declaration**, one YAML file per server, and it
is how `itest plan` learns about agent tools at all.

The contract is one line: **facts in, traits out.**

You state facts — where the server is, which environment variable holds its
token, what a non-existent id looks like, where a mutation is audited. ITest
decides everything else: which tools exist (it asks the server), what calling
each one does (it classifies the live listing and *cross-checks* anything you
declared), and which checks each tool gets (the applies-when table in
`itest/traits/traits.yaml`).

There is no `dangerous: true` field, and there will not be one.

## Where the file goes

```
.itest/tools/<server>.yaml
```

The file name **is** the server name: `server: reference-mcp` must live in
`reference-mcp.yaml`. A mismatch is refused rather than guessed at, because the
path is the address — it is half of every point id and a fragment of every
generated test name.

A project can be **declarations only**. With no Terraform files (`*.tf`,
`*.tf.json`) in the project directory and no `--tf-json`, `itest plan` and
`itest sync` treat the Terraform side as an empty resource set — whether
terraform is not installed, fails, or reports an empty state — and plan the
declared servers. A directory that *has* Terraform files and an empty state is
still refused: there, it means nothing was applied.

## The worked example

The committed declaration for `examples/reference-mcp/` is the reference for the
whole format:
[`examples/reference-mcp/.itest/tools/reference-mcp.yaml`](../examples/reference-mcp/.itest/tools/reference-mcp.yaml).

```yaml
server: reference-mcp

transport:
  kind: stdio                                    # stdio | http
  command: [python, server.py]                   # launched in the project directory
  url_env: REFERENCE_MCP_URL                     # a NAME, never a URL

auth:
  scheme: bearer                                 # none | bearer
  credential_env: REFERENCE_MCP_TOKEN            # a NAME, never a token
  second_tenant_env: REFERENCE_MCP_TOKEN_TENANT_B

tenancy:
  scoped_by: credential                          # credential | parameter | both

identity:
  runs_as: service                               # passthrough | service | per_tenant
  backing_role: reference-mcp-service

audit:
  sink: stderr-json

approval:
  destructive_requires: none                     # none | confirm_param | human | policy

sentinels:
  nonexistent_id: sentinel-cannot-exist-0000     # required
  readable_record: r-1

environments:
  active_allowed_in: [staging]

defaults:
  mutation: detect                               # detect (default) | read | write | destructive | informational
  egress: none                                   # none | {to: ..., data: [...]}
  active: true

tools:
  delete_record:
    approval: confirm_param
    notes: >-
      Reports a miss in its payload instead of raising, so a sentinel id
      returns a successful result.
  enrich:
    egress:
      to: enrichment-provider.example
      data: [email]
```

## The rules, and why each one is there

### A stdio command runs in the project directory

`transport.command` is launched with the **project directory** — the one that
holds the declaration's `.itest/` — as its working directory. A relative path
in it (`server.py`) therefore names a file in that project, whether `itest` is
run from the project, from a parent directory with the project as its base, or
from a pytest subprocess verify started. It never resolves against the
caller's working directory or a repository root. A bare `python` or `python3`
as the first word is the interpreter ITest itself runs under — the one its
dependencies were installed into, and the one verify's pytest run uses — never
whichever `python` is first on `PATH`, so `.venv/bin/itest` works without
activating the virtualenv. Any other first word is passed through untouched.

### A URL and a credential are names, never values

`transport.url_env` and `auth.credential_env` hold the **name** of an
environment variable. ITest resolves it at plan and verify time through the same
path a test credential takes — the shell wins, then a gitignored `.itest/.env` —
and never logs, stores or echoes the value.

A value that is not a plain variable name (anything with a `:`, a `/` or a
space) is a validation error, and **the error does not repeat the value**: a
declaration is committed to the repository and its errors get pasted into
issues. An internal hostname is not something to leak by complaining about it.

When the variable is unset — or the server cannot be launched or reached at
all — plan reports the server as unreachable (`unreachable: REFERENCE_MCP_URL
not set`) and carries on with the rest of the project. It never crashes.

No evidence is not evidence of absence. A server ITest could not ask has not
lost its tools, so the points the manifest already records for it are **held**:
carried forward exactly as last recorded (`last_seen` included), and the tests
covering them are never orphan candidates. The plan says how many points it is
holding under the server's name. `itest sync` then refuses to write anything —
it names the server and exits 1 — unless you pass `--allow-unreachable`, which
applies the rest of the changeset and leaves the held points and their tests as
they were.

### You do not declare the dangerous field

`defaults.mutation` defaults to `detect`, which is the honest answer for a
server you did not write. The mutation class comes from the live tool listing
(the server's own `readOnlyHint` / `destructiveHint` annotations, falling back to
the name pattern) — the same classifier the MCP probe uses.

You *may* write a class, per server or per tool. It is never taken as the
answer; it is **cross-checked**:

| declared | detected | result | recorded provenance |
|---|---|---|---|
| `detect` | anything | detection wins | `detected` |
| a class | the same class | agreement | `confirmed` |
| a class | `unknown` (the server said nothing and the name suggests nothing) | the declaration fills the gap | `declared` |
| a class | a *different* class | **`itest plan` refuses, exit 2** | — |

A disagreement names the tool, the detected class and what decided it, and the
declared class. Nothing is generated and the manifest is not written: one of the
two statements is wrong, and ITest cannot know which.

**The mutation class is also a drift attribute.** Both halves of it are drift,
alongside `schema_hash` and `description_hash`: the class detection resolved,
and a hash of the annotations it was read from. So a tool whose annotations
flip is **changed** even when its name, schema and description are untouched,
and the plan says what moved (`mutation: read → destructive (annotation)`).
Under `detect` the flip is drift, and the checks the new class brings or drops
show up as trait changes. With a declared class, a disagreement still refuses
the plan exactly as above.

### Absence grants nothing

Every optional section defaults to its least claim: no auth, no audit sink,
`destructive_requires: none`, no active environments, `egress: none`. A section
you forget **withholds** a check; it never loosens one. This is the same rule
the environment policy follows — absence is never permission.

`sentinels.nonexistent_id` is the one thing with no safe default, so it is
required. It is what lets a mutating probe prove a guard by being *admitted*
rather than by destroying something.

### A declaration cannot widen the committed policy

Every name in `environments.active_allowed_in` must be an environment that
`.itest/environments.yaml` already permits the `active` tier in. If the policy
does not exist, or does not allow `active` there, loading the declaration is an
error that names the offending environment and the environments that would
work. The policy is the code-reviewed artifact; this file is not.

### Unknown keys are errors

At every level, including inside a per-tool override. A typo would otherwise be
a fact silently not stated, which looks exactly like a fact deliberately
withheld.

## Per-tool overrides

Everything under `tools:` is optional, and `null`/absent means **silent** —
inherit the default. A stated value that happens to match the default is not the
same thing: `egress: none` on a tool is a statement that it egresses nothing,
and that is worth writing when the server-wide default says otherwise.

| key | what it says |
|---|---|
| `mutation` | what you believe calling it does — cross-checked, never trusted |
| `egress` | `none`, or `{to:, data: []}` for a declared external edge |
| `approval` | the gate on this tool specifically |
| `active` | `false` withholds every active-tier check for this tool |
| `notes` | prose a reviewer will read; required with `traits: [none-of-these]` |
| `traits` | override the applies-when table by hand |

`traits: [none-of-these]` says a tool needs no checks at all. It requires
`notes`, because that claim is a person's and it has to be signed where a
reviewer will see it. It cannot sit beside a trait id: either the tool needs no
checks or it needs those ones.

## Which checks a tool gets

Not from this file, and not from code: from
[`itest/traits/traits.yaml`](../itest/traits/traits.yaml), whose `applies_when`
column is a tiny expression over the tool's own attributes (`mutation`,
`egress`, `approval`, `active`, `has_free_form_input`, and three server facts:
`auth.second_tenant_env`, `audit.sink` and `identity.runs_as`). The table is live. Every sync recomputes
every tool's trait set from the current table and the tool's current
attributes, and compares it with the set the manifest recorded
(`traits_planned`). A trait that newly applies gains a check. A trait that
stops applying has its check retired: kept on disk, never run, and reported as
`not_applicable`. The plan lists each change (`+authority.tenant_isolation on server/tool (rule: ...)`).
Engine traits run from one parametrized module per server. Generated traits get
one thin binding per tool, fed by fixtures in a `conftest.py` that is yours.
[docs/traits.md](traits.md) covers the table, what a sync does when it changes,
and the lifecycle states; `itest traits --for <server>/<tool>` shows which rule
decided each trait for one tool.

## What a declaration is *not* for

- It does not say whether a tool is safe. That is what the generated checks are
  for.
- It does not hold a URL, a token, an account id or a hostname you would not
  commit.
- It cannot catch a tool that lies about itself consistently.
  `lookalike_read` in the reference server declares `readOnlyHint=true`, is
  named like a read, and mutates anyway. Nothing in the listing disagrees with
  anything else, so detection is *correct* to call it a read and the cross-check
  has nothing to flag. Catching it takes a call and a look at the store
  afterwards — a behavioural check, not a declaration. It is deliberately left
  undeclared in the example for exactly that reason.
