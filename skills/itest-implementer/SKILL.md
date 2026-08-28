---
name: itest-implementer
description: >-
  Implement ITest's generated pytest stubs, turning [STUB] integration points
  into real assertions against a live AWS account. Use this whenever the user
  asks to implement ITest stubs, fill in or flesh out generated integration
  tests, "make itest verify green", get integration points covered, or
  automate endpoint/integration testing for a Terraform project — and whenever
  they mention itest_tests/, .itest/manifest.yaml, the ITest manifest, itest
  sync/verify, or [STUB] points, even if they never name this skill. Also use
  when someone asks how to test that "A can actually reach B" in deployed
  infrastructure, or wants drift between Terraform and a live account caught
  by tests. Prefer this skill over hand-writing boto3 tests from scratch.
---

# ITest implementer

`itest sync` generates a pytest stub per integration point — a named function
whose body is a single `pytest.skip(...)`. This skill replaces those skips
with real, read-only checks against the deployed account, so `itest verify`
reports points as `[PASS]` instead of `[STUB]`.

You are working on top of two things ITest already produced: the manifest
(`.itest/manifest.yaml`, the inventory and test registry) and the stub file
(`itest_tests/`). Do not reimplement detection or sync — the CLI owns that.

## Workflow

Follow these steps in order. Do not skip ahead to generation.

### 1. Locate

Find `.itest/manifest.yaml`, starting in the current directory. If it is not
there, ask the user where the ITest project lives.

If there is no manifest anywhere, **stop**. Tell the user to run:

```sh
itest plan
itest sync
```

Never fabricate a manifest, and never invent integration points. The manifest
is the only source of truth for what exists.

### 2. Inventory

Parse the manifest. List every test whose `status` is `stub`, grouped by the
`type` of the point it covers (`point_id` links a `TestEntry` to an
`IntegrationPoint`).

For each group, check whether a recipe exists at
`references/recipes/<type>.md`. Five ship today — `sg_edge`, `iam_edge`,
`event_edge`, `route_edge`, and `lb_edge` — one per detector ITest has.

If a type has **no** recipe, say so plainly:

> N points of type `<type>` have no recipe in this skill, so I am skipping
> them. Recipes exist for: sg_edge, iam_edge, event_edge, route_edge, lb_edge.

Then skip them. Never improvise a recipe for a type you have no instructions
for — a guessed assertion that passes is worse than no test.

Read the recipe for each type you are about to implement **before** generating
anything. `event_edge` in particular dispatches on its `mechanism` attribute,
and its five mechanisms take completely different assertions; `lb_edge`
dispatches on `hop`, and is the one recipe whose checks assert liveness
(registered and healthy targets) rather than wiring alone.

### 3. Interview

Ask **once**, batched into a single message. Never ask per test.

First read `.itest/skill-answers.yaml`. Ask only the questions it does not
already answer, so a second run is silent.

| # | Question | Notes |
|---|---|---|
| a | Which AWS profile and region should the checks use? | Named profile, or the default chain. |
| b | Where is the Terraform directory this manifest describes? | The resolver runs `terraform show -json` here. |
| c | Read-only API checks only, or also active probes that send real traffic? | **Read-only is the default.** Active probes require an explicit yes. |
| d | Which environment is this — dev, staging, or prod? | |

If the answer to (d) is **prod**, the read-only tier is forced regardless of
(c). Tell the user you are doing this:

> This is prod, so I am generating read-only checks only — no active probes,
> even though you approved them.

Persist every answer to `.itest/skill-answers.yaml`:

```yaml
aws_profile: my-profile
aws_region: us-east-1
terraform_dir: ../infra
tier: read-only        # or: active
environment: dev
```

Tell the user this file may contain environment details and belongs in their
`.gitignore`.

**Before leaving this step, check that `boto3` imports.** The generated checks
import it, and `itest verify` runs them with the same interpreter ITest itself
runs from. If `boto3` is missing there, every generated test fails collection —
`itest verify` reports those points as `[ERROR]` and exits 2, so you would hand
the user a suite that cannot run.

Find that interpreter and test the import in it:

```sh
python -c "import sys; print(sys.executable)"
python -c "import boto3; print(boto3.__version__)"
```

If the import fails, give the user the exact command for **that** interpreter,
not a bare `pip install boto3` that may land in a different environment:

```sh
/path/to/.venv/bin/python -m pip install boto3
```

Do not generate test bodies until `boto3` imports. A suite that cannot import
its dependencies produces errors, not findings.

### 4. Generate

For each stub that has a matching recipe: read the recipe file, then write the
test body.

**Replace only the `pytest.skip(...)` line.** Preserve the function **name**
and the docstring exactly — the docstring carries the integration point ID,
and `itest verify` maps results back to points by the pytest node id
`path::test_name`. Rename either and the point silently loses its coverage.

The one permitted change to the `def` line is **adding the conftest fixtures
the body needs**:

```python
def test_sg_web_to_db_5432():          # as generated by `itest sync`
def test_sg_web_to_db_5432(ec2, resolve):   # after implementing
```

This is safe because fixture arguments do not appear in a pytest node id — only
`@parametrize` ids do — so `path::test_name` is unchanged and the manifest
mapping still resolves. Do not add `@parametrize` to a generated test: that
*would* change the node id and break the link.

**Open the file each `TestEntry` names in its `path`.** Stubs are routed by
point type — `itest_tests/test_sg_edges.py`, `test_iam_edges.py`,
`test_event_edges.py` — so a project's stubs live across several files. Never
assume a filename: the manifest entry records where its test actually is, and
that is the only place to read it from.

On the first generation, also create `itest_tests/conftest.py` from the shared
template in [`references/conftest.md`](references/conftest.md). One conftest
serves every stub file; do not write one per file.

### 5. Review gate

Show the user every generated test body and the conftest **before running
anything**. Wait for their go-ahead. Do not run `itest verify`, and do not
make a single AWS call, until they have said yes.

### 6. Verify

Run `itest verify`. Report the coverage line before and after:

> was: `3 integration points: 0 passing, 0 failing, 3 stubs`
> now: `3 integration points: 3 passing, 0 failing, 0 stubs`

If a test fails, show the failure and ask the user which it is:

- a **test bug** — the assertion is wrong, or the resolver picked the wrong
  resource; or
- a **real finding** — the deployed infrastructure does not match the
  Terraform, and the test just caught drift.

Never silently weaken an assertion to force green. A failing check that
reflects reality is the tool working.

## Guardrails

- Generated checks are read-only AWS calls (`describe*`/`get*`/`list*`) unless
  the user explicitly approved active probes in the interview.
- Never create, modify, or delete AWS resources. Never run `terraform apply`.
- Never print secret values; if a check touches Secrets Manager, assert on
  metadata (existence, ARN), never on `secret_string`.
- Never modify tests whose status is not `stub`, and never touch a function
  body that no longer contains the generated `pytest.skip` line.
- ITest's ownership hash will mark edited files human-owned. That is correct
  and expected — do not try to restore hashes.

## Scope

Four recipes ship today, one per detector ITest has:

| Recipe | Covers | Assertion rests on |
|---|---|---|
| [`sg_edge`](references/recipes/sg_edge.md) | Security-group reachability | `ec2:DescribeSecurityGroups` |
| [`iam_edge`](references/recipes/iam_edge.md) | Role -> resource grants | `iam:SimulatePrincipalPolicy`, falling back to reading the policy |
| [`event_edge`](references/recipes/event_edge.md) | ESM, DLQ redrive, Lambda permission, S3 notification, EventBridge target | `lambda:ListEventSourceMappings`, `sqs:GetQueueAttributes`, `lambda:GetPolicy`, `s3:GetBucketNotification`, `events:DescribeRule`, `events:ListTargetsByRule` |
| [`route_edge`](references/recipes/route_edge.md) | API Gateway route -> integration | `apigateway:GET` on methods, integrations and stages; `apigatewayv2:GET` on routes, integrations and stages |

All four share one `conftest.py`, from
[`references/conftest.md`](references/conftest.md).

DESIGN.md's Scope ledger is the authoritative list of what ITest detects. When
it grows a detector, that detector gets a recipe file here; this skill's job is
policy — what a good assertion looks like — while the CLI keeps the mechanism.
