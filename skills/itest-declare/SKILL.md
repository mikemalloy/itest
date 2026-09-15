---
name: itest-declare
description: >-
  Onboard an MCP server into ITest by interviewing its owner for facts and
  writing the server's declaration (.itest/tools/<server>.yaml, plus
  .itest/environments.yaml when the project has none). Use this whenever the
  user asks to onboard an MCP server, declare a server, write a declaration,
  "add my MCP server to itest", or asks "what does ITest need to know about my
  server" — and whenever they mention .itest/tools, a tool declaration, or
  wiring an MCP server into itest plan / sync / verify, even if they never name
  this skill. Also use when a project already has a declaration and the user
  wants it brought up to date with the server as it now is. Prefer this skill
  over writing the YAML by hand from the docs.
---

# ITest declare

An MCP server is not in Terraform state, so nothing can infer it: the person
who runs it has to say it exists. That statement is a **declaration**, one
YAML file per server under `.itest/tools/`, and writing it by hand means
reading `docs/declarations.md` with the schema open beside it. This skill
writes it by asking instead. The first server takes a conversation; the second
is a copy with a few facts changed.

The contract is the declaration's own: **facts in, traits out.** You ask how
the server is reached, how callers authenticate, what identity it runs as,
where calls are logged, what gates a destructive tool, which tools call out
and what leaves. You never ask whether a tool is dangerous. The mutation class
is **detected** by `itest plan` from the live listing's annotations and names;
you show the detection back and ask only when the signals disagree. A
developer who self-classifies a destructive tool as read is the failure mode
this skill exists to make structurally impossible.

The shipped exemplar of what this skill produces is
[`examples/reference-mcp/.itest/tools/reference-mcp.yaml`](../../examples/reference-mcp/.itest/tools/reference-mcp.yaml).
Read it before the first interview: its comments are the voice the file you
write should have, and
[`references/walkthrough.md`](references/walkthrough.md) is this interview
run against that server, ending in that file.

Three references drive the steps below:

- [`references/interview.md`](references/interview.md) — the question bank:
  every question, the field it fills, why it is asked (one line you can show
  the owner), and what to do when the answer is "don't know".
- [`references/field-map.md`](references/field-map.md) — every schema field
  and how it gets its value: asked, detected, derived, or a confirmed default.
  When the schema grows, this table grows with it, and a test enforces that.
- [`references/walkthrough.md`](references/walkthrough.md) — the transcript.

## The rules

These are the skill's safety properties. They are not advice.

- **Names, never values.** A URL or a credential is never written into the
  declaration. Environment variable names only. If the user pastes a value,
  refuse to record it, say why — the file is committed and its errors get
  pasted into issues — and ask for the variable name instead. Do not repeat
  the pasted value back, not even to confirm you are refusing it. The schema
  refuses a value that is not a plain variable name and never echoes it;
  you hold the same line one step earlier.
- **Never production.** Name the non-production instance of this server,
  never a production one. The first reach question says so explicitly and
  says why: `itest plan` lists the server's tools (read-only), but the checks
  the declaration unlocks include an active tier that calls tools with
  sentinel arguments, and the only safe target for that is an instance whose
  data does not matter.
- **The policy is the ceiling.** `environments.active_allowed_in` must be a
  subset of what the committed policy already permits. Absence of a policy is
  not permission. If `.itest/environments.yaml` does not exist, you create
  one with a production environment that refuses `active`, and ask the user
  to name their non-production environment(s). You never widen an existing
  policy.
- **Private hosts are an offer, not a default.** `transport.allow_private_hosts:
  true` is offered only when the owner says the URL's host is loopback or
  private (127.0.0.1, localhost, 10/8, 172.16/12, 192.168/16, link-local,
  fc00::/7) — asked as a yes/no about the host behind the variable name,
  never by collecting the URL — and always with this sentence: A real
  deployment never needs it, and a reviewer should see it in the file.
- **Stdio enforcement is a fact, not a preference.** `auth.enforced_over_stdio`
  is asked only for a stdio transport, phrased as a fact: does this server
  check a credential of its own when launched, or does it trust whoever
  launched it? Default `false`. What the answer turns on: over stdio the
  process boundary is the authentication boundary, so the anonymous-refusal
  check (`authority.anonymous`) applies to a stdio server only when its owner
  says it checks a credential itself. `false` withholds the check; it never
  loosens anything.
- **One network step, read-only.** No shell command you run may contact
  anything but the declared server. `itest plan` is the only step that touches
  the network, and it is read-only. You never call a tool yourself, and you
  never run anything against the server's URL outside `itest plan`.
- **Plan, never sync.** This skill never runs `itest sync`. It ends by showing
  the plan and telling the user the next command.

## Workflow

Follow these steps in order. Ask in batches — one message per step, never one
question at a time — and ask nothing the project already answers.

### 1. Locate

Find the project directory: the one that holds `.itest/`, or should. Start
in the current directory; if there is no `.itest/` and no Terraform here, ask
the user where the project lives rather than guessing a parent.

Then check two files:

- `.itest/environments.yaml` — the committed tier policy. Note whether it
  exists and, if it does, which environments permit `active`. You will need
  that in step 6.
- `.itest/tools/<server>.yaml` — an existing declaration for this server. If
  the user has not named the server yet, list what is under `.itest/tools/`
  and ask which one, or whether this is a new server.

**Branch here.** A declaration that exists is a re-run (see
[Re-run mode](#re-run-mode)): you read it, plan, and ask only about the
deltas. No declaration is a fresh onboarding: continue with step 2.

### 2. Reach

Ask, in one message, the reach questions from
[`references/interview.md`](references/interview.md) (Q2.1–Q2.5):

- **Q2.1** the server name — lower-case letters, digits and hyphens; it is
  the file name, half of every point id and a fragment of every generated
  test name.
- **Q2.2** the transport — `stdio` (the launch command, as argv, relative to
  the project directory; a bare `python` first word is the interpreter ITest
  runs under) or `http` (the NAME of the environment variable that will hold
  the **non-production** URL). A stdio server that also serves HTTP may name
  a URL variable too. This is the question that carries the non-production
  instruction: say it, and say why.
- **Q2.3** (http only) whether the host behind that URL variable is loopback
  or private — a yes/no, with the URL itself not to be pasted — and, on yes,
  the `allow_private_hosts` offer with its sentence. Plan does not derive
  the opt-in from the resolved URL; it takes the declaration's word, so a
  local server declared without it lists as unreachable.
- **Q2.4** the sentinel id — a value that cannot name a real record. Explain
  what it is for: it is what makes probing safe. A mutating check carries it
  so the check is proven by being *admitted*, never by destroying something.
  Offer a shape (`sentinel-cannot-exist-0000`) and have the owner confirm it
  cannot exist on their server.
- **Q2.5** how a caller authenticates — `none`, or `bearer` with the NAME of
  the environment variable holding a non-production token. This belongs to
  reach, not authority, because `itest plan` lists the server with that
  credential when it is set and anonymously otherwise; an authenticated
  server declared without it lists as "unreachable". Tell the user to export
  the variable (or put `NAME=<token>` in a gitignored `.itest/.env`) before
  the next step. You never see the value.

Write the **minimal** declaration now — `server`, `transport`, `auth`,
`sentinels`, nothing else — to `.itest/tools/<server>.yaml`, and run plan:

```sh
itest plan
```

This is the step everything after it is informed by: the real tool list,
each tool's detected mutation class and what decided it, which tools take
free-form input, and any disagreement between a tool's annotations and its
name. Show the user the tool list as plan printed it (name and class per
tool), and read `.itest/plan.json` for the two things the human line does not
show: `attributes.mutation_evidence` (`annotation`, `name`, `unknown`, or
`conflict:name-says-<class>`) and `attributes.mutation == "unknown"`. Those
are step 7's inputs; note them now.

If plan reports the server under "Unreachable declared servers", **stop here
with the error**. Show the line plan printed and ask what to fix: a command
that does not launch, a URL variable that is unset, a credential the server
refused. Do not guess at facts for a server you could not list, and do not
write any of the later sections until plan lists it.

### 3. Authority

Ask, in one message, Q3.1–Q3.4:

- **Q3.1** the NAME of a second tenant's credential variable. Explain: without
  it, tenant isolation is recorded as not verifiable — the check needs two
  identities to cross, and with one there is nothing to cross. It is never
  silently skipped: the summary in step 8 says so.
- **Q3.2** what scopes a call to a tenant: the credential, a parameter, or
  both.
- **Q3.3** whose authority the server acts with — the caller's
  (`passthrough`), its own (`service`), or one identity per tenant
  (`per_tenant`) — and, for the last two, the name of the backing role, user
  or service account. A name, not an ARN with an account id in it.
- **Q3.4** (stdio only) `enforced_over_stdio`, in the fact form above.

### 4. Blast radius

This batch is informed by the tool list. Ask Q4.1–Q4.4, naming tools:

- **Q4.1** which of *these* tools call anything outside the owner's own
  systems, and for each, where it goes and which arguments leave. Each answer
  becomes a per-tool `egress: {to:, data: [...]}`. A destination is a host or
  a service name — a fact, not a secret. Tools not named stay silent and
  inherit `egress: none`.
- **Q4.2** what gates a destructive call server-wide: nothing (`none`), a
  confirmation parameter (`confirm_param`), a human (`human`) or a policy
  engine (`policy`). Name the tools plan detected as destructive **by name**
  when you ask, so the owner answers about real tools; if plan detected none,
  say so and record `none`.
- **Q4.3** for each detected-destructive tool, whether its gate differs from
  the server-wide answer. A different gate is a per-tool `approval:`. The
  same gate is silence.
- **Q4.4** where a mutation is recorded — an audit log, a stream, a table —
  as a name. Explain: its presence is what makes the audit check applicable;
  a server with nowhere to look gets no audit check rather than one that
  asserts nothing.

### 5. Containment and observation

Ask Q5.1–Q5.3 in one message:

- **Q5.1** a record that exists on the non-production instance and is safe to
  read (`sentinels.readable_record`), for a happy-path read.
- **Q5.2** a read tool whose output is a stable, comparable view of the
  server's state (`observation.snapshot_tool`): a count, a listing, a version.
  Explain what it is for: this is what lets ITest catch a tool that claims to
  be read-only and mutates anyway — snapshot, one call with sentinel
  arguments, snapshot again — and without it that check is not verifiable
  for this server. Offer the read tools plan listed as candidates; do not
  pick one yourself.
- **Q5.3** any tools that must never be probed, even on the non-production
  instance. Each becomes a per-tool `active: false` with a `notes:` line
  saying why, because that claim is a person's and a reviewer will read it.
  The server-wide `defaults.active: true` is written either way; confirm it.
  An owner who says none of this server's tools may be probed gets
  `defaults.active: false` instead, and the summary says every active-tier
  check is withheld.

### 6. Environments

Ask Q6.1: which environment names may run the active (mutating) tier for this
server. Then enforce the ceiling:

- If `.itest/environments.yaml` exists, every name the owner gives must be
  an environment it defines with `active` in its tiers. A name it does not
  permit is not written; say which environments would work. Never edit the
  policy to admit a name — the policy is the code-reviewed artifact and this
  file is not.
- If it does not exist, create it before writing the declaration, from Q6.2:
  the owner's non-production environment name(s), each permitting
  `[static, readonly, active]`, plus a `prod` entry with
  `tiers: [static, readonly]` and `production: true`. The loader refuses a
  declaration whose `active_allowed_in` names an environment no policy
  permits, so this file has to exist first.

An owner who wants no active tier anywhere leaves the list empty, and every
active-tier check is withheld; say so.

### 7. Conflicts

Go back to the notes from step 2. For each tool whose `mutation_evidence`
starts with `conflict:` — the server's annotations say one class and the
name says another — or whose detected class is `unknown` — the server said
nothing and the name suggests nothing — present **both signals** and ask the
owner what calling it actually does (Q7.1). Never resolve one yourself.

Record the resolution as a per-tool `mutation:` with a `notes:` line quoting
both signals and the owner's reason. Then say what the cross-check will do
with it, because it is not a free choice:

- `unknown` and a declared class: the declaration fills the gap; plan records
  it as `declared`.
- a conflict where the owner agrees with the **annotation**: plan records
  `confirmed`, and the name is the thing to fix on the server.
- a conflict where the owner agrees with the **name**: plan will **refuse**
  (exit 2) — the declaration cannot override a server's own annotation. The
  fix is the annotation, on the server. Write `mutation: detect` for that
  tool with a note saying the annotation is wrong and who owns it, so the
  agreement check (`blast.mutation_class`) keeps reporting the disagreement
  until it is fixed.

An owner who does not know leaves the tool at `detect`; the disagreement
stays a finding in verify rather than a fact in the file.

### 8. Write and show

Write the full declaration in the shipped example's voice: server-level facts
first, `tools:` entries only where a tool differs from the defaults, and a
comment on each section saying what the fact is *for* (not what the key
means — the schema says that). The whole file should fit on one screen so the
next server is a copy-and-edit. Write `defaults:` out in full
(`mutation: detect`, `egress: none`, `active: true`) so the next reader sees
the defaults confirmed rather than assumed.

Then re-run plan and show the result:

```sh
itest plan
```

Show the changeset, then the trait set per tool. Before the first sync there
is no manifest, so `itest traits --for` cannot answer yet; read the trait set
from the plan instead — `.itest/plan.json` carries `traits_planned`, a map
from point id to the trait ids that apply — and print it for every tool,
with `itest traits` beside it for the rule each trait carries. Walk one or
two tools with the owner — a destructive one and one with egress, if there
are such — and say which facts unlocked which checks. After `itest sync`,
`itest traits --for <server>/<tool>` prints the same decision per trait with
the rule that decided it, and you tell the owner so in the hand-off.

State plainly which checks are **not verifiable** because a fact was left
out: no second tenant means no tenant isolation check; no audit sink means no
audit check; no snapshot tool means the observed mutation-class check cannot
run; a stdio server without `enforced_over_stdio` gets no anonymous-refusal
check, by design. Those are the owner's to add later, not yours to invent.

### 9. Hand off

End with the next command and what it will write:

> Next, `itest sync`. It will update `.itest/manifest.yaml` with one `mcp_tool`
> point per tool, write one parametrized engine module per server for the
> engine checks, one thin binding per (tool, generated trait), and a
> `conftest.py` that is yours — written once, never again. Nothing is deleted
> by a later sync; a check that stops applying is retired in place.

That is the end of this skill. Do not run sync.

## Re-run mode

Step 1 found `.itest/tools/<server>.yaml`. Read it. Then fix the baseline
the deltas are measured against **before** running plan, because plan
overwrites `.itest/plan.json`:

- `.itest/manifest.yaml` exists — the project has synced. Plan's own
  sections (`New integration points`, `Changed`, `Orphan candidates`,
  `Orphaned tool overrides`) are the deltas; use them as printed.
- no manifest, but `.itest/plan.json` exists — the declaration was written
  and never synced. Copy that file's tool list (name, `schema_hash`,
  `description_hash`, `annotations_hash`, `mutation`) aside, run plan, and
  diff the new listing against it by name and hash. Plan will print every
  tool as new because the manifest is empty; that is not a delta.
- neither — there is no baseline. Say so, list what plan found, and ask the
  owner which tools are new since the file was written, or to run
  `itest sync` first so the manifest becomes the baseline. Do not guess.

Now run `itest plan`, and present only the deltas:

- **New tools** — listed by the server, absent from the manifest and from
  `tools:`. Show name and detected class. A new tool that differs from the
  server defaults in nothing gets **no entry**: the defaults apply. Ask only
  step 4's egress and gate questions and step 5's hold-out question, scoped
  to the new tools by name.
- **Changed tools** — plan's `Changed` section: a schema, description or
  annotation that moved. Show the line plan printed, and for a mutation
  change show what decided it. Re-ask only the questions the change
  touches, for that tool by name: a class that became destructive re-asks
  its gate (Q4.3); a schema change on a tool with an `egress:` entry
  re-confirms which arguments leave (Q4.1's `data` list, since the old
  names may be gone); an annotation flip that created a conflict goes to
  step 7. A description change alone asks nothing.
- **Orphaned overrides** — `tools:` entries for names the server no longer
  lists (plan's "Orphaned tool overrides"). Ask whether each is a rename not
  yet deployed or a tool withdrawn; remove the entry only when the owner says
  withdrawn, and say so in the changeset.
- **Conflicts** — step 7, including a declared class the server now
  contradicts (plan exits 2 with both classes named).

Patch the file: keep every fact and every comment the owner wrote, change
only the entries the deltas touched, and show the diff before writing. Never
rewrite silently. Then say explicitly what did not change — "no new
tools, no changed tools, no orphaned overrides, no conflicts; the declaration
stands as written" is a complete and correct outcome, and the skill says it
rather than inventing a question to ask.

## Guardrails

- The interview asks for facts. It never asks "is this tool destructive?" or
  "which traits apply?", and it never writes a `traits:` list: which checks a
  tool gets is the table's decision, and a hand-picked list is a reviewer's
  edit, not an interview answer.
- Write only the two files this skill owns: `.itest/tools/<server>.yaml` and,
  when absent, `.itest/environments.yaml`. Never the manifest, never a test
  file, never `.itest/.env`.
- `itest plan` is read-only and the only network-touching step. Do not call
  a tool, curl the URL, or launch the server yourself to "check" anything.
- Every refusal names the field and the rule and stops there. A pasted
  URL or token is not repeated in your reply.
- A server plan could not reach is a stop, not a guess. Facts about a server
  you did not list are not facts.

## Scope

The skill writes what the schema in `itest/core/declarations/schema.py` can
hold, and [`references/field-map.md`](references/field-map.md) is the
complete list. Two things it deliberately does not do: it never writes a
per-tool `traits:` override (see Guardrails), and it never runs `itest sync`
or `itest verify`. Implementing the generated checks' fixtures afterwards is
the `itest-implementer` skill's job, not this one's.
