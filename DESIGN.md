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

## Test addressing
- Canonical form everywhere the tool prints: `path/to/file.py::TestClass::test_name`
  (class segment optional). User input may use shorthand; resolution happens
  against the manifest registry, never by parsing paths blind. Ambiguous
  shorthand lists matches and exits; it never guesses.

## Detector architecture
- Detectors emit typed primitive integration points. Three ship today:
  `sg_edge` (security-group reachability: source, target, protocol, ports,
  direction), `iam_edge` (role -> resource grants: actions ride in attributes,
  with wildcard_action / wildcard_resource / external / managed /
  broad_managed_policy flags; `external: true` is how cross-stack references
  surface), and `event_edge` (event source mappings, SQS DLQ redrive, Lambda
  permissions; `mechanism` attribute). The Scope ledger below is the current
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
- **Egress asymmetry in `sg_edge`.** An ingress rule always produces an edge;
  an egress rule only when it targets another security group, so "allow all
  outbound" is not treated as an integration point. This is what yields exactly
  the intended `internet → ALB:443 → web:80 → db:5432` chain rather than a
  cloud of edges to 0.0.0.0/0.

## Skill layer
- The bundled skill (`skills/itest-implementer/`) is a wrapper over the CLI and
  the manifest: recipes hold policy (what a good assertion for a point type
  looks like), the CLI holds mechanism (detection, sync, verify). The skill
  never reimplements detection or sync logic.

## Stack
- Python 3.11+, typer, pydantic v2 for schema, PyYAML, pytest + boto3 for
  generated tests. No other runtime dependencies without asking.

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
- Event edge detector (event source mapping, DLQ redrive, lambda_permission)
- Plan and state JSON roots both accepted by the plan entry point
- Manifest schema v2 (tier, resource_group, last_duration_seconds — schema
  and v1 migration only; nothing schedules on them yet)
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
- itest-implementer agent skill (interview, read-only default, and a recipe
  per detector: sg_edge, iam_edge, event_edge, over one shared conftest)

Not yet built (do not build without explicit instruction):
- DNS and endpoint-availability detectors
- Parallel / scheduled execution (xdist, resource_group serialization,
  duration packing, change-scoped verify)
- Labels, filtering, and test groups
- itest add, disable/enable, rm
- Saved-plan review flow (plan -out consumed by sync)
- Shorthand address resolution beyond exact match
- Cross-stack / multi-state analysis
- Any server or web UI
