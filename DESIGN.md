# ITest Design Decisions (v0.1)

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
  points, reports point-level coverage ("N points, N tested, N passing,
  N failing, N stubs, N disabled") plus test-level detail. Supports --output
  junit and json. Exit code nonzero on failures.

## Test addressing
- Canonical form everywhere the tool prints: `path/to/file.py::TestClass::test_name`
  (class segment optional). User input may use shorthand; resolution happens
  against the manifest registry, never by parsing paths blind. Ambiguous
  shorthand lists matches and exits; it never guesses.

## Detector architecture
- Detectors emit typed primitive integration points. v0.1 ships ONE detector:
  security-group edges (source, target, protocol, ports, direction, HCL address).
- Point IDs must be stable across runs (derived from resource addresses +
  rule content, not array indices).
- Unknown resource types are reported as "not analyzed", never silently skipped.

## Stack
- Python 3.11+, typer, pydantic v2 for schema, PyYAML, pytest + boto3 for
  generated tests. No other runtime dependencies without asking.

## Development discipline (applies to every task in this repo)
- Use the superhuman skill whenever it is appropriate to the task at hand.
- No cowboy programming: no task, feature, or fix is complete until its test
  cases exist AND have been executed. Before reporting any work as done, run
  the full pytest suite and show the actual output. "It should work" is not
  done; a passing test run is done.
- Every bug fix starts with a failing regression test that reproduces the bug,
  then the fix, then the passing run.
- Never mark a "Done when" criterion satisfied without having literally run
  the commands it names and observed the results.

## Out of scope for v0.1 (do not build)
- IAM/DNS/event detectors, labels/filtering, disable/enable commands,
  `itest add`, saved-plan consumption beyond the implicit flow, shorthand
  resolution beyond exact match, any server or UI, any agent/skill layer.
