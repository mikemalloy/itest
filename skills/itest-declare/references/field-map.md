# Field map: every declaration field and how it gets its value

One row per field of every model reachable from `Declaration` in
`itest/core/declarations/schema.py`. The disposition is one of four:

| disposition | meaning |
|---|---|
| `asked` | an interview question fills it; the note names the question |
| `detected` | `itest plan` decides it from the live listing; never asked |
| `derived` | nothing to ask: a section header, a key set, or a value this skill never writes |
| `default, confirmed` | the schema default is written out and confirmed with the owner |

A test (`tests/test_skill_declare.py`) walks the schema and fails when a field
has no row here, when a row names a field the schema no longer has, or when an
`asked` row cites a question `interview.md` does not define. When the schema
grows, this table is where the new field's interview decision is made.

| schema field | in the file | disposition | filled by / note |
|---|---|---|---|
| `Declaration.server` | `server` | asked | Q2.1 |
| `Declaration.transport` | `transport:` | derived | the section; its fields below |
| `Declaration.auth` | `auth:` | derived | the section; its fields below |
| `Declaration.tenancy` | `tenancy:` | derived | the section; its field below |
| `Declaration.identity` | `identity:` | derived | the section; its fields below |
| `Declaration.audit` | `audit:` | derived | the section; its field below |
| `Declaration.observation` | `observation:` | derived | the section; its field below |
| `Declaration.approval` | `approval:` | derived | the section; its field below |
| `Declaration.sentinels` | `sentinels:` | derived | the section; its fields below |
| `Declaration.environments` | `environments:` | derived | the section; its field below |
| `Declaration.defaults` | `defaults:` | derived | the section; its fields below, always written out |
| `Declaration.tools` | `tools:` | derived | keys are tool names from the live listing plan showed; an entry exists only where a tool differs from the defaults |
| `Transport.kind` | `transport.kind` | asked | Q2.2 |
| `Transport.command` | `transport.command` | asked | Q2.2 (stdio) |
| `Transport.url_env` | `transport.url_env` | asked | Q2.2 (http, or a stdio server that also serves HTTP) — a NAME, never a URL |
| `Transport.allow_private_hosts` | `transport.allow_private_hosts` | asked | Q2.3, offered only for a loopback or private URL; otherwise left `false` |
| `Auth.scheme` | `auth.scheme` | asked | Q2.5 |
| `Auth.credential_env` | `auth.credential_env` | asked | Q2.5 — a NAME, never a token |
| `Auth.second_tenant_env` | `auth.second_tenant_env` | asked | Q3.1 — absent records tenant isolation not verifiable |
| `Auth.enforced_over_stdio` | `auth.enforced_over_stdio` | asked | Q3.4, stdio only; default `false` |
| `Tenancy.scoped_by` | `tenancy.scoped_by` | asked | Q3.2 |
| `Identity.runs_as` | `identity.runs_as` | asked | Q3.3 |
| `Identity.backing_role` | `identity.backing_role` | asked | Q3.3 (service / per_tenant) |
| `Audit.sink` | `audit.sink` | asked | Q4.4 — absent records the audit check not verifiable |
| `Observation.snapshot_tool` | `observation.snapshot_tool` | asked | Q5.2 — absent records the observed mutation-class check not verifiable |
| `Approval.destructive_requires` | `approval.destructive_requires` | asked | Q4.2, naming the detected destructive tools |
| `Sentinels.nonexistent_id` | `sentinels.nonexistent_id` | asked | Q2.4 |
| `Sentinels.readable_record` | `sentinels.readable_record` | asked | Q5.1 |
| `Environments.active_allowed_in` | `environments.active_allowed_in` | asked | Q6.1, enforced as a subset of the committed policy (Q6.2 creates the policy when absent) |
| `Defaults.mutation` | `defaults.mutation` | detected | always written `detect`; the class comes from the live listing and is never asked |
| `Defaults.egress` | `defaults.egress` | derived | always written `none`; egress is stated per tool (Q4.1), never server-wide |
| `Defaults.active` | `defaults.active` | default, confirmed | Q5.3 confirms `true`; `false` only when the owner says no tool may be probed |
| `ToolOverride.mutation` | `tools.<tool>.mutation` | asked | Q7.1, only as a conflict or unknown-class resolution the owner made after seeing both signals; never otherwise written |
| `ToolOverride.egress` | `tools.<tool>.egress` | asked | Q4.1 |
| `ToolOverride.approval` | `tools.<tool>.approval` | asked | Q4.3, only where a destructive tool's gate differs from the server-wide answer |
| `ToolOverride.active` | `tools.<tool>.active` | asked | Q5.3, only for a tool that must never be probed |
| `ToolOverride.notes` | `tools.<tool>.notes` | asked | Q5.3 and Q7.1: the owner's reason, written wherever an override is a person's claim |
| `ToolOverride.traits` | `tools.<tool>.traits` | derived | never written by this skill: the table decides which checks a tool gets, and a hand-picked list is a reviewer's edit, not an interview answer |
| `Egress.to` | `tools.<tool>.egress.to` | asked | Q4.1 — a host or service name |
| `Egress.data` | `tools.<tool>.egress.data` | asked | Q4.1 — the arguments that leave |

## Counts

| disposition | fields |
|---|---|
| asked | 25 |
| detected | 1 |
| derived | 13 |
| default, confirmed | 1 |
