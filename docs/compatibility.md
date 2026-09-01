# Compatibility

Every row below is a real `terraform show -json` state that ITest has been run
against. **Verified** means the bundled `itest-implementer` skill turned every
stub into a read-only check and `itest verify` was green against the live
account. **Plan** means detection only. Resource counts are managed resources
(data sources excluded). Sanitized copies of the AWS-sample states are
committed under `tests/fixtures/aws-samples/` and pinned by
`tests/test_aws_samples.py`, so each row stays true as the code changes.

| Project | Source | Managed resources | Points | Not analyzed | Modules | Result |
|---|---|---|---|---|---|---|
| alex stage 6 — agents | own production (RAG system) | 22 | 14 (12 iam, 2 event) | 16 | none | **Verified 14/14**; drift caught live ([recording](demo/alex-s6-drift-demo.cast)) |
| alex stage 7 — frontend | own production | 21 | 12 (incl. 2 route) | 11 | none | **Verified 12/12** in production, read-only; first live run of the route recipe; both routes flagged `[open]` (auth=NONE), surfacing a deliberate gateway-authorizer decision |
| alex stage 3 — ingestion | own production | 21 | 6 | 15 | none | Plan |
| alex stage 5 — database | own production | 12 | 5 (1 sg, 4 iam) | 7 | none | Plan (sg edge verified live earlier) |
| alex stage 4 — researcher | own production | 7 | 3 | 4 | none | Plan |
| alex stage 2 — sagemaker | own production | 5 | 1 (broad managed policy flagged) | 4 | none | Plan |
| pubhealthllm stage 5 | own production | — | sg_edge | — | none | Verified (first live run) |
| terraform-sqs-lambda | aws-samples/serverless-patterns | 13 (12 module-nested) | 6 (3 iam, 3 event) | 6 | `terraform-aws-modules` lambda + sqs | **Verified 6/6** — twice: once on first contact, again after the s3/eventbridge detectors landed |
| s3-sqs-lambda-terraform | aws-samples/serverless-patterns | 10 | 6 (4 iam, 2 event) | 3 | none | **Verified 6/6**, including the S3 notification edge live |
| lambda-sqs-terraform | aws-samples/serverless-patterns | 6 | 2 (customer-managed policy resolved) | 1 | none | **Verified 2/2** |
| eventbridge-lambda-terraform | aws-samples/serverless-patterns | 6 | 3 (1 iam, 2 event) | 1 | none | **Verified 3/3**, including the EventBridge target edge live |
| ecs-fargate-alb | `terraform-aws-modules/ecs` fargate example | 62 (61 module-nested) | 12 (6 iam, 6 lb) | 37 | `terraform-aws-modules` ecs + alb + vpc | **Verified 7/7 implemented** (5 wildcard-IAM points left as stubs by choice); first live run of the lb_edge liveness check; caught ECS's blue/green traffic flip live (see below); failure drills passed — a detached IAM policy and a scaled-to-zero service were each named precisely, everything else stayed green |

Not run: alex stage 8 and two non-AWS (Azure, GCP) stacks had empty state in
the checkout; one project needed a backend re-init. ITest's detectors are
AWS-only today, so Azure and GCP resources would all report as not analyzed.

## What the AWS samples found

Running ITest on infrastructure it was not built against surfaced six
defects, each fixed with the sample's sanitized state as the regression
fixture:

- `function_name` resolved against any same-named resource; the
  `terraform-aws-modules/lambda` module names the IAM role identically to the
  function, so Lambda permissions pointed at the role.
- Version-qualified and unqualified Lambda permissions collapsed to one point
  id.
- Customer-managed `aws_iam_policy` attachments were opaque "managed policy"
  edges even when the policy document sat in the same state.
- An empty state (initialized, never applied) was reported as malformed JSON.
- The implementer skill's address resolver stripped `count`/`for_each` indices,
  so `aws_sqs_queue.this[0]` could never match.
- `itest sync` never reclassified a hand-implemented stub as `implemented`.

## What the ECS run found: runtime-owned values

The first live run of the lb_edge recipe (ecs-fargate-alb, above) failed on a
perfectly healthy service — and the failure was the tool learning something
true. ECS blue/green deployments **rewrite the listener-rule forward weights
and move the tasks between the two target groups at deploy time**, so a test
that pins "the production group carries weight 100 and holds the healthy
task" asserts a value Terraform recorded but does not own. The recipe now
distinguishes owners: Terraform's claims (the rule forwards to this group,
the health check path, the registered container and port) are asserted
directly; ECS's runtime split is asserted only as an invariant — weights sum
to 100 with a live side, and at least one side of the pair holds a healthy
target. See `skills/itest-implementer/references/recipes/lb_edge.md` §5e.
The subsequent failure drills confirmed the invariant still fails hard when
the service is actually down (`0 running of 0 desired` reported in the
failure message).

## Active-probe recipe coverage

The `http_probe` recipe (the active tier's first) is the one recipe whose
branches cannot all be shown against a real deployment: you cannot ship a
deliberately unguarded mutating endpoint to prod to prove the CRITICAL catch
fires, nor bake a live credential into a repeatable happy-path test, nor make a
production endpoint slow on demand for the latency floor. So a purpose-built
reference API under `examples/reference-api/` carries one route per branch, and
the harness `tests/test_reference_api.py` asserts each branch's outcome —
including the two proven by a RED result (the auth-ordering 404 and the leaky
`/leaky/action` unauthenticated-unsafe-2xx that classifies CRITICAL). That is
the recipe's regression guard and the safe stage for demoing the critical-catch;
see `examples/reference-api/README.md`.

## Resource types ITest does not analyze yet

Seen across the projects above, most frequent first. Every one appears in
`itest plan`'s "Not analyzed" block — nothing is silently skipped.

| Type | Seen in | What it would become |
|---|---|---|
| `aws_sqs_queue_policy`, `aws_s3_bucket_policy` | s3-sqs-lambda, alex 7 | resource-based grant (the other side of an IAM edge) |
| `aws_cloudfront_distribution` | alex 7 | CloudFront → origin edge |
| `aws_vpc_security_group_ingress_rule` / `_egress_rule` (the newer standalone rule resources) | ecs-fargate-alb | sg_edge, same as their `aws_security_group_rule` predecessors |
| `aws_lambda_function` env vars carrying ARNs/URLs | all | implicit configuration edge |

Types with no integration semantics (log groups, S3 objects, versioning and
encryption settings, packaging helpers such as `null_resource` and
`local_file`) are reported and expected to stay unanalyzed.
