# Walkthrough: the interview against `examples/reference-mcp`

The `itest-declare` interview, run as the owner of the reference server
(`examples/reference-mcp/server.py`) in a copy of that directory with both
`.itest/tools/*.yaml` and `.itest/environments.yaml` deleted. Every answer is
taken from `server.py`; every command output is real. The file it ends in
must be the shipped
[`examples/reference-mcp/.itest/tools/reference-mcp.yaml`](../../../examples/reference-mcp/.itest/tools/reference-mcp.yaml)
— a test loads the block below through the real loader and compares both
the facts and the trait set per tool against the shipped one. If they ever
differ, this walkthrough is wrong.

## Step 1 — Locate

```
.itest/                       exists (empty of state)
.itest/environments.yaml      absent  → step 6 creates it
.itest/tools/reference-mcp.yaml  absent  → fresh onboarding
```

No declaration: this is a fresh onboarding, not a re-run.

## Step 2 — Reach

> **Q2.1** What should this server be called in ITest?
> **A.** `reference-mcp`.
>
> **Q2.2** How is the non-production instance reached? Name the non-production
> instance of this server, never a production one: the checks the declaration
> unlocks include an active tier that calls tools with sentinel arguments.
> **A.** Over stdio: `python server.py`, run from this directory. The same
> server also serves streamable HTTP (`server.py --http`); the variable for
> that URL is `REFERENCE_MCP_URL`. There is no production instance of this
> server at all — it exists to be probed.
>
> **Q2.3** *(not asked: the reach is stdio, and the HTTP variable is a name,
> not an address, so nothing here is a loopback URL to offer the opt-in for.)*
>
> **Q2.4** An id that cannot name a real record, confirmed.
> **A.** Ids look like `r-1`; `sentinel-cannot-exist-0000` cannot collide.
>
> **Q2.5** How does a caller authenticate? Name of the variable, never the
> token.
> **A.** Bearer token, over the guarded HTTP mount. The variable is
> `REFERENCE_MCP_TOKEN`. Exported before plan.

The minimal declaration, written so plan can list the server:

```yaml
server: reference-mcp

transport:
  kind: stdio
  command: [python, server.py]
  url_env: REFERENCE_MCP_URL

auth:
  scheme: bearer
  credential_env: REFERENCE_MCP_TOKEN

sentinels:
  nonexistent_id: sentinel-cannot-exist-0000
```

```sh
itest plan
```

```
ITest plan: 8 new, 0 unchanged, 0 orphaned test(s).

New integration points (8):
  + [informational (detected)] reference-mcp -> get_guide
      id=7538e9e6e5a3  hcl=.itest/tools/reference-mcp.yaml
  + [read (detected)] reference-mcp -> search_records
      id=701e5bb76f92  hcl=.itest/tools/reference-mcp.yaml
  + [read (detected)] reference-mcp -> fetch_record
      id=ff8cd3c6119f  hcl=.itest/tools/reference-mcp.yaml
  + [write (detected)] reference-mcp -> create_record
      id=3a6fd655070e  hcl=.itest/tools/reference-mcp.yaml
  + [write (detected)] reference-mcp -> update_record
      id=9357ac9ed396  hcl=.itest/tools/reference-mcp.yaml
  + [destructive (detected)] reference-mcp -> delete_record
      id=49dc4907b3ff  hcl=.itest/tools/reference-mcp.yaml
  + [read (detected)] reference-mcp -> lookalike_read
      id=90bb8861b1f7  hcl=.itest/tools/reference-mcp.yaml
  + [read (detected)] reference-mcp -> enrich
      id=fa74b535a494  hcl=.itest/tools/reference-mcp.yaml

Orphan candidates (0):
  (none)

Not analyzed (0 resource(s)):
  (none)
```

Shown back to the owner, with what decided each class (from
`.itest/plan.json`, `attributes.mutation_evidence`):

```
get_guide        informational  evidence=name        free_form=False
search_records   read           evidence=annotation  free_form=True
fetch_record     read           evidence=annotation  free_form=True
create_record    write          evidence=annotation  free_form=True
update_record    write          evidence=annotation  free_form=True
delete_record    destructive    evidence=annotation  free_form=True
lookalike_read   read           evidence=annotation  free_form=True
enrich           read           evidence=annotation  free_form=True
```

No `conflict:` evidence and no `unknown` class: step 7 will have nothing to
ask. `lookalike_read` is listed as `read` — its annotation and its name agree,
and both are wrong, which no listing can show. That is what step 5's snapshot
tool is for.

## Step 3 — Authority

> **Q3.1** A second tenant's credential, by variable name? Without it,
> tenant isolation is recorded as not verifiable.
> **A.** `REFERENCE_MCP_TOKEN_TENANT_B`.
>
> **Q3.2** What scopes a call to a tenant?
> **A.** The credential: the bearer token decides what you see.
>
> **Q3.3** Whose authority does a tool use, and the backing identity?
> **A.** Its own — `service` — as `reference-mcp-service`.
>
> **Q3.4** Launched over stdio, does this server check a credential of its
> own, or trust whoever launched it?
> **A.** It trusts the launcher. The bearer check is on the HTTP mount only
> (`BearerGuard` wraps the HTTP app; `server.run("stdio")` has no check).
> → `enforced_over_stdio: false`, so `authority.anonymous` does not apply.

## Step 4 — Blast radius

> **Q4.1** Of get_guide, search_records, fetch_record, create_record,
> update_record, delete_record, lookalike_read, enrich — which call anything
> outside your systems, where, and which arguments leave?
> **A.** `enrich` describes an external enrichment provider
> (`enrichment-provider.example`) and the `email` argument is what would
> leave. The reference implementation opens no socket, but the edge is the
> tool's contract.
>
> **Q4.2** Plan detected `delete_record` as destructive. Server-wide, what
> gates a destructive call?
> **A.** Nothing server-wide (`none`). A blanket claim would be false.
>
> **Q4.3** Does `delete_record`'s gate differ?
> **A.** Yes: it takes `confirm: bool` and refuses without it →
> `approval: confirm_param` on the tool. And a note: it reports a miss in its
> payload rather than raising, so a sentinel id returns a successful result.
>
> **Q4.4** Where is a mutation recorded?
> **A.** `stderr-json`.

## Step 5 — Containment and observation

> **Q5.1** A record that exists and is safe to read?
> **A.** `r-1` (the seed store holds `r-1` and `r-2`).
>
> **Q5.2** Of the read tools — search_records, fetch_record, lookalike_read,
> enrich, and get_guide — which returns a stable, comparable view of state?
> **A.** `search_records`: its `total` is the store's size, reported even by
> a query matching nothing.
>
> **Q5.3** Any tools that must never be probed?
> **A.** None. `defaults.active: true` stands.

## Step 6 — Environments

> **Q6.2** There is no `.itest/environments.yaml`. Name your non-production
> environment(s).
> **A.** `staging`.
>
> **Q6.1** Which environments may run the active tier for this server? Only
> names the policy permits `active` in.
> **A.** `staging`.

The policy, written first because the loader refuses a declaration that
names an environment no policy permits:

```yaml
version: 1
environments:
  staging: { tiers: [static, readonly, active] }
  prod:    { tiers: [static, readonly], production: true }
```

## Step 7 — Conflicts

Nothing to ask: no tool had `conflict:` evidence and none was `unknown`.
The owner is told so, and told that `lookalike_read` is not a conflict —
its signals agree — and is deliberately left out of `tools:`.

## Step 8 — Write and show

The full declaration. This block is the shipped file:

<!-- BEGIN reference-mcp.yaml -->
```yaml
# The declaration for examples/reference-mcp/server.py.
#
# FACTS IN, TRAITS OUT. Everything here is something the person who runs the
# server knows: how to reach it, which env var holds its token, what a
# non-existent id looks like. Nothing here says whether a tool is dangerous —
# the mutation class is DETECTED from the live tool listing and only
# cross-checked against a value stated here. `mutation: detect` is the default
# and the honest answer for a server you did not write.
#
# NO URL AND NO TOKEN IS EVER LITERAL. `url_env` and `credential_env` name
# environment variables; ITest resolves them at plan time and never logs them.
server: reference-mcp

transport:
  # The default way this server is probed: a subprocess speaking JSON-RPC on
  # stdin/stdout. It is launched in the project directory (the one holding
  # .itest/, i.e. examples/reference-mcp), so `server.py` is the file beside
  # it whatever directory ITest runs from. A bare `python` is the interpreter
  # ITest runs under (install the example's dependencies there:
  # `pip install -e '.[examples]'`), not whichever python is first on PATH.
  kind: stdio
  command: [python, server.py]
  # The same server also serves streamable HTTP (`server.py --http`). The URL
  # is named, never written: export REFERENCE_MCP_URL to probe that variant.
  url_env: REFERENCE_MCP_URL

auth:
  scheme: bearer
  credential_env: REFERENCE_MCP_TOKEN
  # A second tenant's credential. Its presence is what makes the tenant
  # isolation trait (authority.tenant_isolation) applicable at all: with one
  # identity there is nothing to cross. Leave it out and that check is not generated, rather than
  # generated and quietly passing.
  second_tenant_env: REFERENCE_MCP_TOKEN_TENANT_B

tenancy:
  # This server scopes by credential: the bearer token decides what you see.
  scoped_by: credential

identity:
  # It acts as itself, not as the caller.
  runs_as: service
  backing_role: reference-mcp-service

audit:
  # Where a mutation is recorded. Its presence is what makes the audit trait
  # (blast.audit) applicable; a server with nowhere to write an audit record
  # gets no audit check rather than a check that asserts nothing.
  sink: stderr-json

approval:
  # No server-wide approval policy. `delete_record` has its own gate, declared
  # per-tool below — a blanket claim here would be a false one.
  destructive_requires: none

observation:
  # A read tool whose output is a stable, comparable view of the store:
  # search_records reports the store's size. Its presence is what makes the
  # observed mutation-class check (blast.mutation_class_observed) applicable —
  # the check that catches lookalike_read by behaviour.
  snapshot_tool: search_records

sentinels:
  # An id no record can have. A mutating probe carries it so the probe is
  # proven by being ADMITTED, never by destroying something.
  nonexistent_id: sentinel-cannot-exist-0000
  # A record that exists and is safe to read.
  readable_record: r-1

environments:
  # Where the active (mutating) tier may run. Every name here must be an
  # environment that .itest/environments.yaml already permits `active` in —
  # this list cannot grant a tier the committed policy withholds.
  active_allowed_in: [staging]

defaults:
  mutation: detect
  egress: none
  active: true

tools:
  delete_record:
    # The tool takes `confirm: bool` and refuses without it. This is the gate,
    # stated; the *destructive* class itself is detected, not declared.
    approval: confirm_param
    notes: >-
      Reports a miss in its payload instead of raising, so a call with
      sentinels.nonexistent_id returns a successful result and the
      "an anonymous caller reached this tool" catch can fire without
      destroying anything.

  enrich:
    # The one declared egress edge: it describes an external data provider.
    # (The reference implementation calls nothing — an example that phoned home
    # would make the suite depend on the network.) Declaring it is what makes
    # the egress trait (blast.egress) applicable to this tool and no other.
    egress:
      to: enrichment-provider.example
      data: [email]

  # `lookalike_read` is deliberately ABSENT from this section. It declares
  # readOnlyHint=true, it is named like a read, and it mutates anyway. Nothing
  # in the tool listing disagrees with anything else, so detection here is
  # correct to call it a read and the cross-check has nothing to flag. Catching
  # it needs a call and a look at the store afterwards — a behavioural check
  # (the active-tier "mutation class observed" check), not a declaration. Do not "fix" this by declaring it.
```
<!-- END reference-mcp.yaml -->

```sh
itest plan
```

```
ITest plan: 8 new, 0 unchanged, 0 orphaned test(s).

New integration points (8):
  + [informational (detected)] reference-mcp -> get_guide
      id=7538e9e6e5a3  hcl=.itest/tools/reference-mcp.yaml
  + [read (detected)] reference-mcp -> search_records
      id=701e5bb76f92  hcl=.itest/tools/reference-mcp.yaml
  + [read (detected)] reference-mcp -> fetch_record
      id=ff8cd3c6119f  hcl=.itest/tools/reference-mcp.yaml
  + [write (detected)] reference-mcp -> create_record
      id=3a6fd655070e  hcl=.itest/tools/reference-mcp.yaml
  + [write (detected)] reference-mcp -> update_record
      id=9357ac9ed396  hcl=.itest/tools/reference-mcp.yaml
  + [destructive (detected) [approval confirm_param]] reference-mcp -> delete_record
      id=49dc4907b3ff  hcl=.itest/tools/reference-mcp.yaml
  + [read (detected)] reference-mcp -> lookalike_read
      id=90bb8861b1f7  hcl=.itest/tools/reference-mcp.yaml
  + [read (detected) [egress enrichment-provider.example]] reference-mcp -> enrich
      id=fa74b535a494  hcl=.itest/tools/reference-mcp.yaml

Orphan candidates (0):
  (none)

Not analyzed (0 resource(s)):
  (none)
```

The two flags that moved — `[approval confirm_param]` on `delete_record`,
`[egress enrichment-provider.example]` on `enrich` — are the two per-tool
overrides, shown back by plan.

The trait set per tool, from `.itest/plan.json`'s `traits_planned` (there is
no manifest yet, so `itest traits --for` cannot answer until the owner syncs):

```
get_guide       informational  authority.backing_least_privilege, blast.mutation_class, blast.mutation_class_observed, change.inventory, change.schema_drift, change.description_drift
search_records  read           authority.tenant_isolation, authority.backing_least_privilege, blast.mutation_class, blast.mutation_class_observed, containment.parameter_scope, containment.expression_passthrough, containment.output_hygiene, change.inventory, change.schema_drift, change.description_drift
fetch_record    read           authority.tenant_isolation, authority.backing_least_privilege, blast.mutation_class, blast.mutation_class_observed, containment.parameter_scope, containment.expression_passthrough, containment.output_hygiene, change.inventory, change.schema_drift, change.description_drift
create_record   write          authority.tenant_isolation, authority.backing_least_privilege, blast.mutation_class, blast.audit, containment.parameter_scope, containment.expression_passthrough, containment.output_hygiene, change.inventory, change.schema_drift, change.description_drift
update_record   write          authority.tenant_isolation, authority.backing_least_privilege, blast.mutation_class, blast.audit, containment.parameter_scope, containment.expression_passthrough, containment.output_hygiene, change.inventory, change.schema_drift, change.description_drift
delete_record   destructive    authority.tenant_isolation, authority.backing_least_privilege, blast.mutation_class, blast.destructive_gating, blast.audit, containment.parameter_scope, containment.expression_passthrough, containment.output_hygiene, change.inventory, change.schema_drift, change.description_drift
lookalike_read  read           authority.tenant_isolation, authority.backing_least_privilege, blast.mutation_class, blast.mutation_class_observed, containment.parameter_scope, containment.expression_passthrough, containment.output_hygiene, change.inventory, change.schema_drift, change.description_drift
enrich          read           authority.tenant_isolation, authority.backing_least_privilege, blast.mutation_class, blast.mutation_class_observed, blast.egress, containment.parameter_scope, containment.expression_passthrough, containment.output_hygiene, change.inventory, change.schema_drift, change.description_drift
```

Walked with the owner: the second tenant (Q3.1) unlocked
`authority.tenant_isolation` on every non-informational tool; the audit sink
(Q4.4) unlocked `blast.audit` on the write and destructive tools; the egress
edge (Q4.1) unlocked `blast.egress` on `enrich` alone; the snapshot tool
(Q5.2) unlocked `blast.mutation_class_observed` on every read and
informational tool — including `lookalike_read`, which is the point. Not
verifiable, by design: `authority.anonymous` on every tool, because the reach
is stdio and Q3.4 said the server trusts its launcher.

After `itest sync`, `itest traits --for` gives the same decision with the
rule beside it. For the two tools walked above:

```
reference-mcp/delete_record: destructive (detected) [approval confirm_param]
  id=49dc4907b3ff  declared in .itest/tools/reference-mcp.yaml
  11 of 15 traits apply (table c2e5c2154241), decided from the manifest's recorded attributes.

  does not apply   authority.anonymous                 AUTH-1     refuses anonymous            engine     readonly  rule: transport.kind == http or auth.enforced_over_stdio                       ASI03, LLM02, semgrep-server-4, CWE-306
  APPLIES          authority.tenant_isolation          AUTH-2     tenant isolation             generated  active    rule: auth.second_tenant_env present and mutation != informational             ASI03, LLM02, semgrep-server-18, CWE-639
  APPLIES          authority.backing_least_privilege   AUTH-3     acts as declared identity    generated  active    rule: identity.runs_as present                                                 ASI03, LLM03, CWE-269
  does not apply   authority.delegation                AUTH-4     caller identity passthrough  generated  active    rule: identity.runs_as == passthrough                                          ASI03, semgrep-server-8, CWE-441
  APPLIES          blast.mutation_class                BLAST-1    mutation class               engine     readonly  rule: always                                                                   ASI02, LLM03, ACS-AgBOM
  does not apply   blast.mutation_class_observed       BLAST-1b   mutation class (observed)    engine     active    rule: mutation in [read, informational] and observation.snapshot_tool present  ASI02, LLM03
  APPLIES          blast.destructive_gating            BLAST-2    destructive gating           generated  active    rule: mutation == destructive                                                  ASI02, LLM03
  does not apply   blast.egress                        BLAST-3    external egress              engine     readonly  rule: egress != none                                                           ASI04, LLM02, semgrep-server-21
  APPLIES          blast.audit                         BLAST-4    audit record                 generated  active    rule: mutation in [write, destructive] and audit.sink present                  ACS-AgBOM, CWE-778
  APPLIES          containment.parameter_scope         CONTAIN-1  parameter scope              engine     active    rule: mutation != informational                                                ASI02, semgrep-server-19, CWE-639
  APPLIES          containment.expression_passthrough  CONTAIN-2  expression passthrough       engine     active    rule: has_free_form_input                                                      ASI05, semgrep-server-20, semgrep-server-22, CWE-77, CWE-89
  APPLIES          containment.output_hygiene          CONTAIN-3  output hygiene               engine     readonly  rule: mutation != informational                                                LLM02, LLM10, CWE-209
  APPLIES          change.inventory                    CHANGE-1   inventory                    engine     readonly  rule: always                                                                   ASI04, LLM04, semgrep-client-1, ACS-AgBOM
  APPLIES          change.schema_drift                 CHANGE-2   schema drift                 engine     readonly  rule: always                                                                   ASI04, LLM04, ACS-AgBOM
  APPLIES          change.description_drift            CHANGE-3   description drift            engine     readonly  rule: always                                                                   ASI04, ASI01, LLM01, semgrep-client-2
```

```
reference-mcp/enrich: read (detected) [egress enrichment-provider.example]
  id=fa74b535a494  declared in .itest/tools/reference-mcp.yaml
  11 of 15 traits apply (table c2e5c2154241), decided from the manifest's recorded attributes.

  does not apply   authority.anonymous                 AUTH-1     refuses anonymous            engine     readonly  rule: transport.kind == http or auth.enforced_over_stdio                       ASI03, LLM02, semgrep-server-4, CWE-306
  APPLIES          authority.tenant_isolation          AUTH-2     tenant isolation             generated  active    rule: auth.second_tenant_env present and mutation != informational             ASI03, LLM02, semgrep-server-18, CWE-639
  APPLIES          authority.backing_least_privilege   AUTH-3     acts as declared identity    generated  active    rule: identity.runs_as present                                                 ASI03, LLM03, CWE-269
  does not apply   authority.delegation                AUTH-4     caller identity passthrough  generated  active    rule: identity.runs_as == passthrough                                          ASI03, semgrep-server-8, CWE-441
  APPLIES          blast.mutation_class                BLAST-1    mutation class               engine     readonly  rule: always                                                                   ASI02, LLM03, ACS-AgBOM
  APPLIES          blast.mutation_class_observed       BLAST-1b   mutation class (observed)    engine     active    rule: mutation in [read, informational] and observation.snapshot_tool present  ASI02, LLM03
  does not apply   blast.destructive_gating            BLAST-2    destructive gating           generated  active    rule: mutation == destructive                                                  ASI02, LLM03
  APPLIES          blast.egress                        BLAST-3    external egress              engine     readonly  rule: egress != none                                                           ASI04, LLM02, semgrep-server-21
  does not apply   blast.audit                         BLAST-4    audit record                 generated  active    rule: mutation in [write, destructive] and audit.sink present                  ACS-AgBOM, CWE-778
  APPLIES          containment.parameter_scope         CONTAIN-1  parameter scope              engine     active    rule: mutation != informational                                                ASI02, semgrep-server-19, CWE-639
  APPLIES          containment.expression_passthrough  CONTAIN-2  expression passthrough       engine     active    rule: has_free_form_input                                                      ASI05, semgrep-server-20, semgrep-server-22, CWE-77, CWE-89
  APPLIES          containment.output_hygiene          CONTAIN-3  output hygiene               engine     readonly  rule: mutation != informational                                                LLM02, LLM10, CWE-209
  APPLIES          change.inventory                    CHANGE-1   inventory                    engine     readonly  rule: always                                                                   ASI04, LLM04, semgrep-client-1, ACS-AgBOM
  APPLIES          change.schema_drift                 CHANGE-2   schema drift                 engine     readonly  rule: always                                                                   ASI04, LLM04, ACS-AgBOM
  APPLIES          change.description_drift            CHANGE-3   description drift            engine     readonly  rule: always                                                                   ASI04, ASI01, LLM01, semgrep-client-2
```

## Step 9 — Hand off

> Next, `itest sync`. It will write `.itest/manifest.yaml` with eight
> `mcp_tool` points, `itest_tests/tools_reference-mcp/` with one engine
> module for the readonly tier and one for the active tier, one thin binding
> per (tool, generated trait) — tenant isolation, identity, destructive
> gating, audit — and a `conftest.py` that is yours. Then
> `itest verify --environment staging`.

The skill stops here. It does not run sync.

## The re-run

With the file in place and no sync yet, the baseline is the previous
`.itest/plan.json` (eight tools, hashes recorded). Copied aside, then:

```sh
itest plan
```

Plan prints the same eight tools as new — the manifest is still empty, so
that is not a delta — and the diff against the baseline by name and hash is
empty. The skill asks nothing and says:

> No new tools, no changed tools, no orphaned overrides, no conflicts. The
> declaration stands as written, and nothing in it was touched.

After a sync, the same re-run reads plan's own sections instead:

```
ITest plan: 0 new, 8 unchanged, 0 orphaned test(s).
```
