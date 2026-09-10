# Example pipeline files

Supporting files for
[`.github/workflows/pipeline-example.yml`](../../.github/workflows/pipeline-example.yml),
the copyable post-apply workflow described in
[docs/pipeline.md](../pipeline.md).

| File | What it is | Where it belongs in your repo |
|---|---|---|
| `environments.yaml` | the committed tier policy | `.itest/environments.yaml`, committed |

The environment gate has two halves and only one of them is here. The
**policy** above is committed and code-reviewed. The **binding** — which
environment this particular run is pointed at — is machine-local and
gitignored, so CI has to supply it: the example workflow passes it as
`--environment "$ITEST_ENVIRONMENT"` from a job-level environment variable, and
a runner-local `.itest/environment` file written in a step does the same job.
Neither is a secret and neither should be committed.

`tests/test_pipeline_example.py` executes the workflow's steps against
`tests/fixtures/alex/alex-s6.json` and asserts every `itest` command in the
workflow is one it ran, so this example cannot drift from what the CLI does.
