# The question bank

Every question the `itest-declare` interview asks, grouped by workflow step.
Each entry carries four things: the schema field(s) it fills, the phrasing,
why it is asked (one line the skill may show the owner), and what happens when
the answer is "don't know". The last is the important one: **absence grants
nothing**, so an unknown fact withholds a check rather than loosening one, and
the skill says which check is withheld instead of skipping it silently.

Three dispositions for "don't know":

- **must answer** — the schema has no default and no safe one exists.
- **leave default** — the schema's default is the least claim; write nothing.
- **record not verifiable** — leave the field absent and say, in the summary,
  which check that withholds and why.

Ask each step's questions in one batched message. Never one question at a
time, and never a question the project already answers.

The phrasing below never asks "is this tool destructive?" or "which traits
apply?". Those are not facts an owner supplies; they are what ITest decides
from the facts below and the live listing.

## Step 2 — Reach

### Q2.1 — the server name
- **Fills:** `server` (and the file name, `.itest/tools/<server>.yaml`)
- **Ask:** What should this server be called in ITest? Lower-case letters,
  digits and hyphens only.
- **Why:** The name is the file name, half of every point id and a fragment
  of every generated test name, so it has to be stable and path-safe.
- **If "don't know":** must answer. Offer the server's own `name` from its
  `initialize` response, slugged, once plan has listed it — but the owner
  confirms it.

### Q2.2 — the transport
- **Fills:** `transport.kind`, `transport.command`, `transport.url_env`
- **Ask:** How is the **non-production** instance of this server reached?
  Either `stdio` — give the launch command as argv (`[python, server.py]`),
  relative to the project directory — or `http` — give the NAME of the
  environment variable that will hold its URL, never the URL. A stdio server
  that also serves HTTP can name a URL variable as well. Name the
  non-production instance of this server, never a production one.
- **Why:** `itest plan` lists the server's tools (read-only), but the checks
  the declaration unlocks include an active tier that calls tools with
  sentinel arguments; the only safe target for that is an instance whose data
  does not matter. A relative stdio command resolves against the project
  directory, and a bare `python` first word is the interpreter ITest runs
  under.
- **If "don't know":** must answer. Nothing can be listed without it.

### Q2.3 — private hosts (conditional)
- **Fills:** `transport.allow_private_hosts`
- **Ask:** *(only when the URL the owner describes is loopback or private:
  127.0.0.1, localhost, 10/8, 172.16/12, 192.168/16, link-local, fc00::/7)*
  The private-host guard refuses that address by default. For a local
  reference or test server, the declaration can opt in with
  `allow_private_hosts: true`. A real deployment never needs it, and a
  reviewer should see it in the file. Opt in?
- **Why:** The guard is the SSRF rule. It stays on unless the file says
  otherwise, and every plan names a server that sets it.
- **If "don't know":** leave default (`false`). Plan will refuse the private
  host and say so; that is the answer showing itself.

### Q2.4 — the sentinel id
- **Fills:** `sentinels.nonexistent_id`
- **Ask:** Give an id that cannot name a real record on this server — a
  value no lookup will ever hit (for example `sentinel-cannot-exist-0000`).
  Confirm it cannot exist.
- **Why:** It is what makes probing safe. A mutating check carries it, so the
  check is proven by being *admitted* — the call went through — never by
  destroying something.
- **If "don't know":** must answer. Offer the shape above; the owner confirms
  it cannot collide with their id format.

### Q2.5 — how a caller authenticates
- **Fills:** `auth.scheme`, `auth.credential_env`
- **Ask:** How does a caller authenticate to the non-production instance:
  `none`, or a `bearer` token? For `bearer`, the NAME of the environment
  variable that holds a non-production token. Export it (or put
  `NAME=<token>` in a gitignored `.itest/.env`) before the next step; the
  value never goes in the file and never in this conversation.
- **Why:** Plan lists the server with the credential when the variable is
  set and anonymously otherwise. An authenticated server declared without
  its credential lists as unreachable.
- **If "don't know":** leave default (`none`). If plan then reports the
  server refused the listing, the server does check a credential, and this
  question is asked again.

## Step 3 — Authority

### Q3.1 — a second tenant's credential
- **Fills:** `auth.second_tenant_env`
- **Ask:** Is there a second tenant on the non-production instance, with a
  credential of its own? If so, the NAME of the environment variable holding
  it.
- **Why:** Its presence is what makes the tenant-isolation check possible at
  all: with one identity there is nothing to cross.
- **If "don't know":** record not verifiable. The field stays absent,
  `authority.tenant_isolation` is not generated, and the summary says so.
  Never silently skipped.

### Q3.2 — what scopes a tenant
- **Fills:** `tenancy.scoped_by`
- **Ask:** What decides which tenant's data a call can reach: the credential
  alone, a parameter in the arguments, or both?
- **Why:** A tenant-isolation check has to vary whatever scopes the call. If
  a tenant id rides in the arguments, the check varies the argument too.
- **If "don't know":** leave default (`credential`).

### Q3.3 — whose authority the server acts with
- **Fills:** `identity.runs_as`, `identity.backing_role`
- **Ask:** When a tool runs, whose authority does it use against the systems
  behind it — the caller's own (`passthrough`), a single identity of the
  server's (`service`), or one identity per tenant (`per_tenant`)? For the
  last two, the name of the role, user or service account. A name, not an
  ARN carrying an account id.
- **Why:** The identity checks compare what the server acts as with what it
  says it acts as; `passthrough` also asks whether the caller's identity is
  really the one delegated.
- **If "don't know":** leave default (`passthrough`). Both identity checks
  are generated either way and wait on the owner's conftest fixture; say so.

### Q3.4 — enforcement over stdio (stdio only)
- **Fills:** `auth.enforced_over_stdio`
- **Ask:** *(stdio transports only)* Launched as a subprocess, does this
  server check a credential of its own when launched, or does it trust
  whoever launched it?
- **Why:** Over stdio the process boundary is the authentication boundary, so
  the anonymous-refusal check applies to a stdio server only when its owner
  says it checks a credential itself. `false` withholds the check; it never
  loosens anything.
- **If "don't know":** leave default (`false`).

## Step 4 — Blast radius

### Q4.1 — which tools call out
- **Fills:** `tools.<tool>.egress` (`to`, `data`)
- **Ask:** Of these tools *(name every tool plan listed)*, which call
  anything outside your own systems? For each: where does it go (a host or
  service name), and which of its arguments leave?
- **Why:** A declared egress edge is what makes the egress check applicable
  to that tool and no other. A destination is a fact, not a secret.
- **If "don't know":** record not verifiable. Tools not named stay silent
  and inherit `egress: none`; no egress check is generated for them, and the
  summary says so.

### Q4.2 — the server-wide destructive gate
- **Fills:** `approval.destructive_requires`
- **Ask:** Plan detected these tools as destructive: *(list them by name, or
  say none were)*. Server-wide, what has to happen before a destructive call
  goes through: nothing (`none`), a confirmation parameter (`confirm_param`),
  a human (`human`), or a policy engine (`policy`)?
- **Why:** The destructive-gating check proves the gate holds by knocking
  with a sentinel id; it needs to know what the gate is.
- **If "don't know":** leave default (`none`). It is a statement — no gate —
  not a gap; the gating check is still generated for every destructive tool
  and its fixture will ask again.

### Q4.3 — per-tool gates
- **Fills:** `tools.<tool>.approval`
- **Ask:** For each of those destructive tools *(by name)*: is its gate
  different from the server-wide answer?
- **Why:** A blanket claim that is false for one tool is a false claim. A
  different gate is stated on the tool; the same gate is silence.
- **If "don't know":** leave default (silence; the server-wide answer
  applies).

### Q4.4 — where a mutation is recorded
- **Fills:** `audit.sink`
- **Ask:** Where is a mutation recorded — a log stream, a table, a file, an
  audit service? As a name.
- **Why:** Its presence is what makes the audit check applicable. A server
  with nowhere to look gets no audit check rather than one that asserts
  nothing.
- **If "don't know":** record not verifiable. `blast.audit` is not generated
  for this server's write and destructive tools, and the summary says so.

## Step 5 — Containment and observation

### Q5.1 — a readable record
- **Fills:** `sentinels.readable_record`
- **Ask:** Give the id of a record that exists on the non-production instance
  and is safe to read.
- **Why:** A happy-path read needs something real to read.
- **If "don't know":** record not verifiable. Happy-path reads that need a
  real record are withheld; the sentinel-only checks still run.

### Q5.2 — the snapshot tool
- **Fills:** `observation.snapshot_tool`
- **Ask:** Which of these read tools *(name the tools plan detected as read
  or informational)* returns a stable, comparable view of the server's state
  — a count, a listing, a version — that would move if something wrote?
- **Why:** This is what lets ITest catch a tool that claims to be read-only
  and mutates anyway: snapshot through this tool, call the tool under test
  once with sentinel arguments, snapshot again. Without it that check is not
  verifiable for this server.
- **If "don't know":** record not verifiable. `blast.mutation_class_observed`
  is withheld from every tool on the server — never generated and quietly
  passing — and the summary says the lying-read catch cannot run here.

### Q5.3 — tools that must never be probed
- **Fills:** `tools.<tool>.active`, `tools.<tool>.notes`; confirms
  `defaults.active`
- **Ask:** Are there tools here that must never be called by a check, even on
  the non-production instance? For each, one line saying why. Otherwise the
  server-wide default stands: active-tier checks may run wherever the policy
  permits them.
- **Why:** `active: false` withholds every active-tier check for that tool.
  The claim is a person's, so it is signed with a note a reviewer will read.
  `defaults.active: true` is written out so the next reader sees it confirmed.
- **If "don't know":** leave default (`true`; no per-tool entries). An owner
  who says *none* of this server's tools may be probed gets
  `defaults.active: false`, and the summary says every active-tier check is
  withheld.

## Step 6 — Environments

### Q6.1 — where the active tier may run
- **Fills:** `environments.active_allowed_in`
- **Ask:** In which environment(s) may the active (mutating) tier run for
  this server? Only names the committed policy already permits `active` in
  are accepted: *(list them, from `.itest/environments.yaml`)*.
- **Why:** `environments.active_allowed_in` must be a subset of what the
  committed policy already permits. Absence of a policy is not permission.
  The policy is the code-reviewed artifact; this file is not.
- **If "don't know":** leave default (an empty list). The active tier never
  runs for this server, and the summary says every active-tier check is
  withheld until an environment is named.

### Q6.2 — the policy file (only when absent)
- **Fills:** `.itest/environments.yaml` (the committed tier policy, not a
  declaration field)
- **Ask:** *(only when `.itest/environments.yaml` does not exist)* There is no
  environment policy in this project yet. Name your non-production
  environment(s); each gets `tiers: [static, readonly, active]`. A `prod`
  entry with `tiers: [static, readonly]` and `production: true` is written
  regardless.
- **Why:** The loader refuses a declaration whose `active_allowed_in` names
  an environment no policy permits, so the policy has to exist first — and a
  production environment that refuses `active` has to be in it from birth.
- **If "don't know":** must answer, or leave Q6.1 empty and write the policy
  with `prod` alone. No active tier runs until a non-production environment
  is named.

## Step 7 — Conflicts

### Q7.1 — a tool whose class the signals do not settle
- **Fills:** `tools.<tool>.mutation`, `tools.<tool>.notes`
- **Ask:** *(one per tool plan flagged)* For `<tool>`, the server's
  annotations say `<class A>` and its name says `<class B>` — or: the server
  declares no annotations and the name suggests nothing. What does calling
  it actually do: read, write, destructive, or informational?
- **Why:** One of the two signals is wrong and ITest cannot know which.
  Recording the owner's answer with both signals quoted turns a finding into
  a signed statement — and a declared class is only ever cross-checked: an
  answer that contradicts a server's own annotation is refused by plan, and
  the fix is the annotation on the server.
- **If "don't know":** leave default (`detect`). The disagreement stays a
  finding in verify (`blast.mutation_class` fails on it) rather than a fact
  in the file. Never resolve one yourself.
