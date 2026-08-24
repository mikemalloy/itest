# Handoff — session continuation notes

Last updated: 2026-08-24. This note orients a fresh session (web or otherwise)
picking up the ITest repo. For *why* things are the way they are, read
[DESIGN.md](DESIGN.md) first — it is the design constitution and binds all work
in this repo, including its "Development discipline" section (tests must exist
**and be run** before anything is called done).

## Where things stand

**v0.1 is complete and green.** The project was built in nine steps, one commit
each (`git log --oneline` reads `Prompt 0` … `Prompt 8`). All three commands
work end-to-end against a checked-in fixture with no AWS needed.

- `itest plan` — [itest/core/planner.py](itest/core/planner.py),
  [itest/core/mermaid.py](itest/core/mermaid.py)
- `itest sync` — [itest/core/syncer.py](itest/core/syncer.py),
  [itest/core/stubgen.py](itest/core/stubgen.py)
- `itest verify` — [itest/core/verifier.py](itest/core/verifier.py),
  [itest/core/_pytest_report.py](itest/core/_pytest_report.py)
- Schema — [itest/core/manifest.py](itest/core/manifest.py)
- Detector (only one in v0.1) — [itest/core/detectors/](itest/core/detectors/)
- CLI wiring — [itest/cli.py](itest/cli.py)

**23 tests pass.** Suites live in [tests/](tests/).

## Set up and verify (do this first)

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q              # expect: 23 passed
```

Then confirm the tool itself works (see [README.md](README.md) quickstart):

```sh
itest plan   --tf-json tests/fixtures/simple-web-app-plan.json
itest sync   --auto-approve --tf-json tests/fixtures/simple-web-app-plan.json
itest verify
```

Running the quickstart from the repo root creates `.itest/` and `itest_tests/`.
In *this* repo those are gitignored (they are throwaway demo output); in a real
consumer project you would commit `.itest/manifest.yaml` and `itest_tests/`.

## Conventions in force

- **Commit granularity:** one commit per task, message = the task title.
- **No cowboy programming:** nothing is "done" until its tests are written and
  actually run with visible passing output (DESIGN.md).
- **Scope discipline:** v0.1 is deliberately one detector + three commands.
  The "Out of scope for v0.1" list in DESIGN.md is a hard boundary — if asked
  to add a fourth command or a new dependency, point back to DESIGN.md.

## What is intentionally NOT built (the roadmap, all planned)

From DESIGN.md "Out of scope" and README "Roadmap": additional detector tiers
(IAM, endpoint availability, DNS, events), composite/service detectors, labels
& filtering, `itest add`, `disable`/`enable`, a saved-plan review flow, and any
agent/skill layer. None of these exist yet — do not claim they do.

## Two design calls worth knowing before you extend

1. **Fixture uses resolved security-group ids.** A real fresh-create
   `terraform plan` renders SG references as unknown-until-apply; the checked-in
   fixture uses resolved `sg-…` ids so SG-to-SG mapping is deterministic. See
   [tests/fixtures/README.md](tests/fixtures/README.md).
2. **Detector egress asymmetry.** Ingress rules always produce an edge; egress
   rules only when they target another SG (so "allow all outbound" is not
   treated as an integration point). This is what yields exactly the intended
   `internet → ALB:443 → web:80 → db:5432` chain. Rationale is in the module
   docstring of [itest/core/detectors/sg_edges.py](itest/core/detectors/sg_edges.py).

## Extension point

New detectors implement the interface in
[itest/core/detectors/base.py](itest/core/detectors/base.py)
(`detect(plan_json) -> list[IntegrationPoint]`, declare `handled_types`,
register in `DETECTORS`). [sg_edges.py](itest/core/detectors/sg_edges.py) is the
reference implementation.
