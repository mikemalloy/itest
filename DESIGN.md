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

Not yet built (do not build without explicit instruction):
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
