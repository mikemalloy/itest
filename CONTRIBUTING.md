# Contributing to ITest

Read [DESIGN.md](DESIGN.md) before starting a task. Its architecture,
invariants, development discipline, and scope ledger are the source of truth.
Check existing issues and pull requests before taking on work; discuss new
scope with the maintainer rather than implementing items listed as not yet built.

## Development setup

Use Python 3.11 or newer, from the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

The dev dependencies include the local reference applications used by tests.
The CI suite is hermetic: it needs no AWS credentials or deployed infrastructure;
some tests start reference servers on localhost.

## Required gates

Run all three commands before reporting a task as complete:

```sh
ruff check .
ruff format --check .
pytest
```

Include the actual results in the pull request. A targeted test is useful while
developing, but does not replace the full suite. Do not claim a criterion passed
without running its command and observing the result.

For a bug fix, first write and run a failing regression test that reproduces the
problem. Apply the fix, run that test again, then run all gates. Documentation
changes still require the gates; do not invent a failing runtime test for a
documentation-only task.

Keep changes focused on one task, with one commit per task. Lead the commit
message with the task title. Update the scope ledger in DESIGN.md when shipped
scope changes. Keep unrelated fixes out of the pull request.

Ruff configuration lives in pyproject.toml. If an intentional pattern needs an
exception, use a targeted per-line `# noqa: RULE` and explain why, never a blanket
ignore. Generated `.itest/` and `itest_tests/` output is excluded from linting;
do not treat it as authored source.

## Sharing examples safely

Never paste raw Terraform state, plan output, or credentials into issues, pull
requests, or fixtures. Use synthetic examples or sanitize artifacts with
`itest redact`; inspect the sanitized output before sharing it. Declarations
name environment variables, not their secret values. Do not commit local
environment bindings or generated artifacts.

## AI assistance

AI-assisted contributions are welcome. Say so in the pull request. The person
submitting is responsible for having run the gates, read the whole diff, and
checked that nothing sensitive or generated is in it. The gates' actual output
in the PR is the evidence, whoever or whatever wrote the code.

## Pull request checklist

- Link the issue and describe the change and its scope.
- Explain the regression test for a bug fix.
- Include the actual output of the three required gates and any limitations.
- Update DESIGN.md's scope ledger where applicable.
- Check the diff for unrelated changes, generated files, and sensitive data.
- There is no PR template yet; this checklist is the template.

Do not mark a task complete based on expected behavior alone: executed tests and
observed results are the completion evidence.
