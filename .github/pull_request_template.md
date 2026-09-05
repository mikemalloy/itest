## What this changes

<!-- One or two sentences. Link the issue: Closes #NNN -->

## Discipline checklist

- [ ] Regression test written **first** (or explain why not)
- [ ] `ruff check .` clean
- [ ] `ruff format --check .` clean
- [ ] `pytest` green locally
- [ ] Semgrep clean (`--config .semgrep/itest.yml`)
- [ ] DESIGN.md scope ledger updated if a detector/recipe changed
- [ ] No raw state/plan/credentials in tests, fixtures, or PR text
- [ ] Load-bearing invariants upheld (or the change to one is called out)

## Notes for the reviewer

<!-- Anything non-obvious: a tradeoff, a follow-up issue, a thing to check. -->
