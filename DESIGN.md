# ITest Design Decisions

ITest is a local-first CLI that analyzes Terraform configurations, extracts
integration points, generates test stubs, and verifies deployed infrastructure.

## Architecture
- Engine-as-library: all logic lives in the `itest` Python package; the CLI is a
  thin command layer (typer) over it. No server. Truth lives in the repo.
- The manifest file `.itest/manifest.yaml` is the single shared artifact:
  inventory of integration points, test registry, ownership hashes, labels,
  disable state. It must remain human-readable and diffable.
- All commands must support machine-readable output (--output json) in addition
  to human terminal output.

## Invariants
- **The amount of generated code a human must maintain is proportional to the
  facts only a human could supply, never to the number of tools.**
- **Ownership.** A generated file whose content still matches its ownership hash
  is ITest's and may be regenerated; a file a human edited is frozen — ITest
  reports it, never rewrites it. A file that is human-owned from birth (a
  declared server's `conftest.py`) carries no ownership hash at all.
- **Nothing generated is ever deleted.** A check that no longer applies is
  retired in place and reported; an orphan stays an orphan.
- **No hardcoded trait rules.** `itest/traits/traits.yaml` is the only source of
  which checks a declared tool gets.

## Command semantics (mirror Terraform's plan/apply model)
- `itest plan`: reads `terraform show -json` output plus the existing manifest,
  detects integration points, prints a proposed changeset (new points, orphaned
  tests, unchanged), writes the proposal to `.itest/plan.json`, and emits a
  Mermaid diagram. Plan never modifies test files.
- `itest sync`: consumes the plan (running one implicitly if absent), updates
  the manifest, generates pytest stubs for new integration points. Pauses for
  confirmation unless --auto-approve. NEVER modifies or deletes a test file
  whose content hash differs from the recorded ownership hash (human-modified).
  Orphaned tests are flagged in the manifest, never deleted.
- `itest verify`: runs the pytest suite, maps results back to integration
  points, and reports point-level coverage plus test-level detail. Supports
  --output junit and json. Exit code is 1 on failures and 2 on errors or a
  config problem. Point status precedence is fail > error > pass > stub.
  Real output, from the synced simple-web-app fixture:

  ```
  3 integration points: 0 passing, 0 failing, 0 errored, 3 stubs, 0 orphaned tests.
  Ran 3 tests in 0.21s

  Points:
    [STUB] 0.0.0.0/0 -> aws_security_group.alb (tcp:443 ingress)
    [STUB] aws_security_group.alb -> aws_security_group.web (tcp:80 ingress)
    [STUB] aws_security_group.web -> aws_security_group.db (tcp:5432 ingress)
  ```
- `itest add`: registers an **existing** test function in the manifest against
  an **existing** point (`--point`/`--file`/`--function`/`--tier`). It never
  declares a new point — detection reads Terraform, not a filename — so an
  unknown point id is refused, not created. The function must be defined in the
  file (checked by AST, never by import), and a duplicate `(path, function)` is
  rejected. `--tier` is required: the caller states what kind of test this is.
  The entry is **human-owned from birth** (its ownership hash is the file's, its
  status read from the body), so sync's append-only, never-relocate guarantees
  keep a round-trip from rewriting, moving, or de-registering it. Registered
  tests join verify — and tier gating — exactly like synced ones.

## Test addressing
- Canonical form everywhere the tool prints: `path/to/file.py::TestClass::test_name`
  (class segment optional). User input may use shorthand; resolution happens
  against the manifest registry, never by parsing paths blind. Ambiguous
  shorthand lists matches and exits; it never guesses.

## Detector architecture
- Detectors emit typed primitive integration points. Five ship today:
  `sg_edge` (security-group reachability: source, target, protocol, ports,
  direction), `iam_edge` (role -> resource grants: actions ride in attributes,
  with wildcard_action / wildcard_resource / external / managed /
  broad_managed_policy flags; `external: true` is how cross-stack references
  surface), and `event_edge` (event source mappings, SQS DLQ redrive, Lambda
  permissions; `mechanism` attribute). A fourth, `route_edge`, covers API
  Gateway: a route -> what it invokes, across both the REST (v1) and HTTP API
  (v2) shapes, with `auth: NONE` surfaced as `[open]` the way
  `wildcard_resource` is surfaced on an IAM edge. A fifth, `lb_edge`, covers
  the load balancer / container spine. The Scope ledger below is the current
  record of what exists.
- Every place a point is printed (plan changeset, Mermaid labels, stub names
  and docstrings) dispatches on point type via `itest/core/points.py`. A new
  detector must add its type there, never leave another command printing
  `None`.
- The plan-JSON entry point accepts either a plan root (`planned_values`)
  or a state root (`values`) and errors naming both when neither is present.
- Generated stubs are routed to `itest_tests/test_<type>s.py`, derived from the
  point type in one place (`stubgen.stub_file_for`), so a new detector needs no
  routing change. Ownership hashes, human-modified detection, and function-name
  uniqueness are all per file. Routing applies to new stubs only: a test already
  in the manifest keeps its recorded path and is never moved.
- **Customer-managed policy resolution in `iam_edge`.** A policy binding whose
  ARN matches an `aws_iam_policy` in the same document is parsed and emits real
  edges carrying `via_policy`, rather than one opaque `managed` edge — the
  document is readable, so guessing is unnecessary; a policy AWS owns, or one
  another stack owns, stays opaque.
- Point IDs must be stable across runs (derived from resource addresses +
  rule content, not array indices).
- Unknown resource types are reported as "not analyzed", never silently skipped.
- **Two hops, one type, in `lb_edge`.** The chain listener -> target group ->
  service is emitted as two kinds of edge under one point type, keyed by the
  `hop` attribute, so each link is independently verifiable: a listener can
  forward correctly into a group nothing has registered into, and an ECS
  service can be wired to a group no listener names. A group nothing feeds
  emits an edge to `(empty)` rather than nothing at all — an ALB routing into
  an empty group is the finding, and silence would hide it.
- **Blue/green alternate groups in `lb_edge`.** An ECS service's
  `load_balancer` block can name a second group in
  `advanced_configuration.alternate_target_group_arn`. That group is fed, so
  reading only `target_group_arn` would report a live standby group as empty.
  `deployment_role` records which side an edge describes and is what keeps
  the two edges' ids distinct.
- **Context claimed without an edge in `lb_edge`.** `aws_ecs_cluster` and
  `aws_ecs_task_definition` are in `handled_types` but emit no edge of their
  own: they are resolved onto the service's edges as `cluster` and
  `task_definition`. A cluster does not route to anything, so an edge for it
  would be an invention; claiming them is honest only because they are
  genuinely read.
- **Egress asymmetry in `sg_edge`.** An ingress rule always produces an edge;
  an egress rule only when it targets another security group, so "allow all
  outbound" is not treated as an integration point. This is what yields exactly
  the intended `internet → ALB:443 → web:80 → db:5432` chain rather than a
  cloud of edges to 0.0.0.0/0.

## Presentation contract
- **The render functions are the canonical output.**
  `planner.render_changeset`, `verifier.render_human`,
  `redact.render_findings` and `cli.render_verify_line` are the single source
  of what ITest says. `itest/core/style.py` is a *colorizer over* that text —
  line-pattern rules that add style spans — never a re-layout. There are no
  Rich tables or panels: a table would decide the content's shape, and the
  content is not styling's to decide.
- **The invariant, enforced by test:** `strip_ansi(styled) == plain`, byte for
  byte, on real output from the committed fixtures. Wrapping, padding or
  truncating a line is a content change, so every print passes
  `soft_wrap=True` — a Rich console attached to a non-terminal defaults to 80
  columns, and a real plan's module-nested lines run past 180.
- **Off by default for anything but a terminal.** When stdout is not a TTY, or
  `NO_COLOR` is set (any value), or `--no-color` is passed, `style.decorate`
  returns its argument and the CLI's `echo` helper is byte-for-byte the
  `typer.echo` call it replaced. Terminal detection is Rich's own
  (`Console.is_terminal`); nothing forces a terminal except the documented
  `ITEST_FORCE_COLOR` escape hatch, which exists so a demo or an acceptance
  run can capture colour through a pipe.
- **Machine-read paths are never styled**: `--output json` and `--output
  junit`, the JUnit note, the sanitized `redact` document, `--version`, and
  the "Wrote sanitized copy" status note keep calling `typer.echo` directly.
  Exit codes are untouched by presentation.
- **`verify --redact` styles after redaction, never before**, so an account id
  is gone before an escape is added.
- Styling is deliberately restrained — this is a tool read next to `terraform
  plan`, not a dashboard. Rollups carry weight, finding-class flags
  (`[open]`, `BROAD`, `DENY`, `wildcard_action`, `wildcard_resource`) carry
  colour, and `external` stays unstyled because a cross-stack reference is a
  fact, not a finding. The pytest traceback block is excluded before the rules
  run: it is already formatted, and it can contain the very words the flag
  pass looks for.

## Environment profiles

The gate that lets an active (mutating) test tier exist safely is the AND of two
artifacts: a committed **policy** (`.itest/environments.yaml`) saying which tiers
each named environment may run, and a local **binding** (a `--environment` flag,
else the `.itest/environment` file) saying where this checkout is pointed. A
tier runs only when both allow it. The policy is code-reviewed shared state; the
binding is machine-local and gitignored like `skill-answers.yaml`, because where
a checkout points is not a project fact.

- **Absence is never permission.** No policy, no binding, or a tier simply left
  off an environment's list all resolve to the *safe floor* — static and
  readonly only. A project with no policy file behaves exactly as before, output
  byte-identical; an active tier never runs without both an explicit policy that
  allows it and an explicit binding to an environment that allows it. This is
  why the reporting is append-only: the `[GATED]` point lines and the `, N gated`
  rollup clause appear only when something is actually gated (the same pattern as
  the resurrection clause in plan), so the common line is unchanged to the byte.
- **Refusal at load, not at run.** An unknown tier, an unsupported version, a
  binding naming an undefined environment, and a `production: true` environment
  that lists `active` are all hard errors raised when the policy is loaded —
  verify refuses to start. A policy that would loose a mutating test in
  production therefore cannot be committed quietly against a green suite.
- **Production by name.** An environment named exactly `prod` or `production` is
  treated as production even when the flag is absent; name it that and ITest
  believes you, so the active-tier refusal fires whether or not someone
  remembered to write `production: true`.
- **Gated, not skipped.** A disallowed test is removed from collection, never
  skipped at runtime. A file whose every live test is gated is `--ignore`-d and
  never imported; a gated test sharing a file with allowed siblings is
  `--deselect`-ed — the module still imports for the siblings, but the gated
  test never runs. The strong never-import guarantee holds for a dedicated
  active-tier file, which is where a mutating probe belongs.

## Probes

Active-tier tests need code that touches a live endpoint, and it lives in its
own `itest/probes/` package — deliberately apart from the detectors and the CLI.
A detector reads Terraform and never touches the network; a probe touches the
network and never reads Terraform. Keeping them in separate packages means the
read-only analysis path has no accidental route to sending a request.

- **The HTTP probe is bodyless by construction.** `probe()` has no `body`
  parameter at all. An unauthenticated probe of an unsafe method carrying an
  empty body is the least dangerous way to ask "does auth stop me?": a refusal
  proves the guard, an acceptance is the finding, and nothing was sent to act
  on. It also never follows a redirect (a 3xx is an answer — it records the
  `Location` and returns the status), never retries, and adds no authorization
  header of its own; a caller may pass headers explicitly, which is how an
  authenticated happy-path test supplies a credential.
- **A timeout is a distinct outcome.** Exceeding the caller's timeout raises a
  typed `ProbeTimeout` carrying the elapsed time. A non-2xx status (401, 403,
  404, a recorded 3xx) is a normal result, not an exception — a hang and a
  refusal must never look alike to the caller.

## Declarations (Ring 3)

A detector reads Terraform and infers points. An MCP server is not in Terraform
state, so nothing can infer it — the person who runs it has to say it exists, in
`.itest/tools/<server>.yaml`. **Facts in, traits out**: the file states how to
reach the server, which environment variables hold its url and tokens (never the
values — a pasted URL or token is refused, and the refusal does not repeat it),
what a non-existent id looks like, where a mutation is audited. ITest decides the
rest. **The developer never declares the dangerous field.** The mutation class is
*detected* from the live tool listing (the same classifier the MCP probe uses) and
a declared class is only ever cross-checked: agreement is recorded as
`confirmed`, a gap detection left as `declared`, `detect` as `detected`, and a
**disagreement refuses the whole plan** — exit 2, the tool and both classes
named, nothing written, because one of the two statements is wrong and ITest
cannot know which. Identity is `(server, tool name)` and nothing else;
`schema_hash` and `description_hash` are *attributes*, so a reworded description
is a **changed** entry in the changeset rather than a new point — an id that
moved would orphan the tests covering the tool and re-stub them empty, which is
the one way this drift could go unnoticed. Which checks a tool gets is **data,
not code**: `itest/traits/traits.yaml` holds one row per trait and an
`applies_when` expression over the tool's own attributes, and an unknown
attribute or unreadable clause there is an error rather than a quietly false
clause — a typo must not delete a check. Its `tier` column is what routes each
stub: active-tier checks go in a per-server file of their own, because verify can
`--ignore` a fully-gated file (never importing it) but can only `--deselect` a
gated test sitting beside runnable siblings. Planning a declared server is the
one place `itest plan` reaches past the filesystem, so the probe is imported
inside that step and a server it cannot reach is a line in the changeset, never
an exception: one broken server must not blind the rest of the project. Nor may
it erase what is known: an unreachable server's recorded points are **held** as
last recorded and their tests are never orphan candidates, and sync refuses to
write (exit 1, the server named) unless `--allow-unreachable` accepts the gap.

**The table is live.** Every plan recomputes every declared tool's trait set from
the current table and the tool's current attributes, and diffs it against the
`traits_planned` the manifest recorded, so a table edit or a tool that moved is
a "Trait changes" line — `+A2 on server/tool (rule: ...)`,
`−B2 on server/tool (mutation changed)` — and the manifest's `trait_table_hash`
(over the table's parsed content) says when the table itself changed. The
mutation class as detected (the resolved class and an `annotations_hash`) is a
drift attribute: an annotation flip is `changed`, never a quiet `unchanged`. A
trait that stops applying **retires** its test (`retired: true`: kept on disk,
never run, reported `not_applicable`) and restores the same entry if it applies
again. `itest traits` / `itest recipes` print the table, one tool's decisions,
and the recipes it names, from the manifest and the table alone.

### Engine checks and generated checks
Each trait row says who runs it (`kind`). An **engine** check needs nothing but
the tool's point and a way to reach its server, so there is no per-tool code for
it: each declared server gets two engine modules at most — one for the active
tier, one for every other tier — each a single test the
engine parametrizes over the manifest at collection time and runs through
`itest.checks.run_engine_check` — a tool added to the manifest is covered without
regenerating anything, and each case is registered as `test_engine[<tool>-<id>]`
so verify maps its result. A **generated** check needs a fact only a human can
supply (a second tenant's record, the identity the server should act as), so it
gets one thin binding per tool — a frozen docstring (point id, trait id, the
schema hash it was generated against), one import, one call to
`run_generated_check` — fed by a `<trait>_fixtures` fixture in the server's
`conftest.py`, which sync writes once and never again. What a human maintains is
that one conftest, whatever the number of tools. The library the generated code
binds to (`itest/traits/runtime.py`) is ITest's, so fixing it fixes every check.

**Lifecycle states.** Every check in verify's tool ledger carries a `state`:
`current` (ITest-owned, generated against the current schema), `hand_edited`
(ownership hash differs), `stale` (hand-edited *and* generated against a schema
the tool no longer has), `not_applicable` (retired), `orphan` (the tool is gone),
and `recipe_newer` (defined, not yet emitted: nothing records a recipe version).
VERIFIED is a coverage claim — every planned trait needs a counted check that
passed — and stale, not-applicable and orphaned checks do not count; any stale
check makes the page AT RISK and leads the exceptions. `docs/traits.md` is the
reference.

## Skill layer
- The bundled skill (`skills/itest-implementer/`) is a wrapper over the CLI and
  the manifest: recipes hold policy (what a good assertion for a point type
  looks like), the CLI holds mechanism (detection, sync, verify). The skill
  never reimplements detection or sync logic.

## Stack
- Python 3.11+, typer, pydantic v2 for schema, PyYAML, rich for terminal
  styling of human output, pytest + boto3 for generated tests. No other
  runtime dependencies without asking.

## Development discipline (applies to every task in this repo)
- No cowboy programming: no task, feature, or fix is complete until its test
  cases exist AND have been executed. Before reporting any work as done, run
  the full pytest suite and show the actual output. "It should work" is not
  done; a passing test run is done.
- Every bug fix starts with a failing regression test that reproduces the bug,
  then the fix, then the passing run.
- Never mark a "Done when" criterion satisfied without having literally run
  the commands it names and observed the results.
- **Commit granularity:** one commit per task, and the commit message leads
  with the task title.

### Linting
- `ruff check` and `ruff format --check` must pass before any task is called
  done, alongside the test suite.
- Ruff is configured in `pyproject.toml` (target py311, line length 88, rule
  sets E, F, I, B, UP). Generated demo output (`.itest/`, `itest_tests/`) is
  excluded — it is tool output, not authored source, and must not gate lint.
- When a rule fights an intentional pattern, silence it with a targeted
  per-line `# noqa: RULE` plus a comment saying why. Never a blanket ignore.

## Scope ledger

Shipped:
- Security-group edge detector
- IAM edge detector (role -> resource, wildcard / cross-stack / managed flags)
- Event edge detector (event source mapping, DLQ redrive, lambda_permission,
  s3_notification, eventbridge_target)
- API Gateway route detector (REST + HTTP API)
- Load balancer / container detector (`lb_edge`: listener and listener-rule
  forwards, weighted forwards, redirect and fixed-response actions, target
  group -> ECS service including the blue/green alternate group, target group
  attachments, and the empty-target-group finding)
- Plan and state JSON roots both accepted by the plan entry point
- Manifest schema v2 (tier, resource_group, last_duration_seconds). `tier` is
  now consumed by verify's environment gating; resource_group and
  last_duration_seconds remain schema-only until a runner uses them.
- Environment profiles (`itest/core/environments.py`): a committed tier policy
  (`.itest/environments.yaml`) and a local binding (`--environment` flag or
  `.itest/environment` file). verify gates disallowed tiers out of collection
  and reports `[GATED <env>]` plus a `, N gated` rollup clause. Absence resolves
  to the safe floor (static, readonly); policy problems are refused at load;
  an environment named prod/production is production by name.
- plan / sync / verify with changeset, ownership hashes, orphan flagging,
  and orphan resurrection
- Per-type stub files (`itest_tests/test_<type>s.py`, one per point type,
  with ownership hashes and human-modified detection tracked per file)
- Mermaid diagram generation (`.itest/diagram.mmd`)
- Machine-readable output (`--output json` on plan and verify, `--output
  junit` on verify)
- `itest redact` — sanitizes plan/state JSON for safe sharing (sensitive_values,
  Lambda env allowlist, credential patterns, account pseudonymization, `--check`)
- Ruff lint/format as part of Development discipline
- Rich style layer over the canonical strings (`itest/core/style.py`), wired
  into plan / sync / verify / redact, with `--no-color`, `NO_COLOR`, and the
  `strip_ansi(styled) == plain` invariant under test
- itest-implementer agent skill (interview, read-only default, and a recipe
  per detector: sg_edge, iam_edge, event_edge, route_edge, lb_edge, over one
  shared conftest; lb_edge is the first recipe asserting liveness — registered
  and healthy targets — rather than wiring alone). http_probe is the sixth
  recipe and the active tier's first: an OpenAPI-driven per-endpoint 401/403
  sweep with a latency floor, each operation registered with `itest add --tier
  active` onto its route_edge point, gated out of production by policy.
- Active probes (`itest/probes/http.py`): a bodyless, redirect-recording,
  no-retry, timeout-typed single-shot HTTP probe for active-tier endpoint
  checks. Lives in its own package, with no path from the read-only analysis.
- `itest add`: register an existing test function onto an existing point
  (`itest/core/register.py`). Existing points only — it never declares a point;
  AST-validated; the entry is human-owned from birth and survives a sync
  round-trip untouched.
- http_probe reference API (`examples/reference-api/`): a FastAPI app with one
  route per branch of the http_probe recipe, and a harness
  (`tests/test_reference_api.py`) that runs recipe-shaped probes against it under
  uvicorn and asserts an expected-outcome table — including the two branches
  proven by a RED result (the auth-ordering 404 and the deliberate
  `/leaky/action` unauthenticated-unsafe-2xx classified CRITICAL). It is the
  recipe's regression guard and the safe stage for the critical-catch demo,
  because those branches cannot be shown on a real prod system. The harness also
  drives the probes through the recipe's real base-URL resolution: a synthetic
  `terraform show -json` state is walked by the detectors and the base URL
  resolved from it, never pasted. fastapi + uvicorn are an `[examples]`
  optional-dependency group (mirrored into dev deps so the suite runs); the CLI
  itself pulls no web framework.
- Readiness page (`itest report --html`): one self-contained HTML file built
  from `verify --output json` plus the manifest and injected into the committed
  template (`itest/report/templates/readiness.html`) at seven exact markers.
  `itest/report/model.py` decides no markup and `render.py` decides no meaning;
  string templating only, so no new runtime dependency. **Nothing on the page
  is illustrative**: every number, name and status comes from verify JSON, the
  manifest, or a fixed label, and a section whose source is absent renders
  empty-but-named ("No agent tools declared") rather than as sample data — a
  test asserts none of the design artifact's example data can reach output. A
  stub-only run is AT RISK, never VERIFIED: a stub is not coverage, so a green
  stamp over "0 of 26 verified" is the one thing the page must never print.
  Trends and the since-line appear only with `--since <prior manifest>`, never
  as "steady"; `--redact` reuses verify's own scrubber rather than adding a
  second one. The verdict is not an exit code — that stays verify's job. Tests
  pin the extracted data blocks, not pixels. Four things verify cannot supply
  today are modelled Optional and listed in `docs/report.md`: the tool ledger
  (P30/P31), the not-analyzed census (it lives in the plan), the release
  commit, and which registered test was the anonymous probe — the last is why
  the API sweep shows one Status column rather than an
  Unauthenticated/Authenticated pair holding the same neutral value.

- Tool declarations (`itest/core/declarations/`, `docs/declarations.md`): the
  schema and loader for `.itest/tools/<server>.yaml` (unknown keys refused at
  every level, url and credentials by env-var NAME, `active_allowed_in` unable to
  widen the committed environment policy), `mcp_tool` points built from a live
  `tools/list` with the mutation-class cross-check and per-field provenance,
  plan's **changed** / **orphaned tool override** / **unreachable server**
  sections (all append-only, so a terraform-only plan is byte-identical), the
  applies-when table (`itest/traits/traits.yaml`, eleven traits in four families at P30)
  driving one stub per (tool, trait) with the active tier in its own file, and
  `itest add --server` to register a hand-written test onto a tool point by name.
  No manifest schema bump: `mcp_tool` is a new value of an existing field.
- The live trait table (`docs/traits.md`): a `kind` column (engine | generated),
  A3/A4 identity rows and C3 output hygiene (fourteen traits), loader validation
  (unique ids, known families, kind, tier, parseable `applies_when` over known
  attributes — at load), and `trait_table_hash()` over the parsed table. Every
  sync recomputes every tool's trait set and diffs it against `traits_planned`
  (P30 manifests fall back to their per-trait tests); gains and retirements are
  plan lines, `is_noop` counts them, and the mutation class is a drift
  attribute. Manifest additions (`trait_table_hash`, `traits_planned`,
  `trait`, `retired`) are optional and written only when set, so P30 and
  declaration-free manifests load and round-trip byte for byte — no schema
  bump.
- Lifecycle states per check in verify's tool ledger (`tools.servers[]`, now
  emitted), stale blocking VERIFIED, and state tags plus a "needs attention"
  line on the readiness page.
- `itest traits` (table, `--for <server>/<tool>`, `--json`) and `itest recipes`.
- Thin binding stubs for generated traits, one parametrized engine module per
  server for the active tier and one for every other tier, and a human-owned conftest written once
  (`itest_tests/tools_<server>/`); `itest/traits/runtime.py` is what they bind
  to, and `itest/checks/` holds the agreed contract stub until 31A's library
  replaces it.
- Packaging: `traits.yaml` and the readiness template ship in the wheel and are
  read through `importlib.resources`.
- MCP probe SSRF guard: `McpTarget` refuses a loopback / link-local / metadata
  host and any non-http(s) scheme before the transport is built, reusing the HTTP
  probe's own check, with `allow_private_hosts=True` for a deliberate local
  target.

Not yet built (do not build without explicit instruction):
- Tool recipes and the declaration interview (P31/31A): the recipe files the
  trait table names (`itest recipes` lists which exist) and the skill flow that
  writes a declaration by asking. Until the engine-check library lands, every
  engine check skips as not implemented and every generated binding skips on
  its unfilled fixture — reported `not_run`, never pass.
- Regeneration of ITest-owned stubs on recipe change (and so `recipe_newer`:
  nothing records the recipe version a check was generated from).
- A declaration reconfirm flag: a way for a reviewer to accept a `changed` or
  `stale` finding in the declaration rather than by editing the check.
- The PR loop: sync opening a pull request for the checks it adds or retires.
- Behavioural mutation checks. A tool that lies *consistently* —
  `lookalike_read` declares `readOnlyHint=true`, is named like a read, and
  mutates — cannot be caught by any reading of `tools/list`, so the cross-check
  correctly has nothing to flag. Catching it takes a call and a look at the store
  afterwards, which is a recipe's job.
- DNS and endpoint-availability detectors
- EKS. Explicitly out of scope: a Kubernetes Service, Ingress, or Deployment
  is not in Terraform state, so there is nothing for a detector to read. The
  `aws_eks_cluster` resource is the container the objects live in, not the
  wiring between them, and inventing edges from it would mean guessing.
- Parallel / scheduled execution (xdist, resource_group serialization,
  duration packing, change-scoped verify)
- Labels, filtering, and test groups
- itest disable/enable, rm (itest add now ships, in its existing-points-only
  form; declaring new points from the CLI remains out of scope)
- Saved-plan review flow (plan -out consumed by sync)
- Shorthand address resolution beyond exact match
- Cross-stack / multi-state analysis
- Any server or web UI
