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
| alex stage 7 — frontend | own production | 21 | 10 | 11 | none | Plan |
| alex stage 3 — ingestion | own production | 21 | 6 | 15 | none | Plan |
| alex stage 5 — database | own production | 12 | 5 (1 sg, 4 iam) | 7 | none | Plan (sg edge verified live earlier) |
| alex stage 4 — researcher | own production | 7 | 3 | 4 | none | Plan |
| alex stage 2 — sagemaker | own production | 5 | 1 (broad managed policy flagged) | 4 | none | Plan |
| pubhealthllm stage 5 | own production | — | sg_edge | — | none | Verified (first live run) |
| terraform-sqs-lambda | aws-samples/serverless-patterns | 13 (12 module-nested) | 6 (3 iam, 3 event) | 6 | `terraform-aws-modules` lambda + sqs | **Verified 6/6** |
| s3-sqs-lambda-terraform | aws-samples/serverless-patterns | 10 | 5 (4 iam, 1 event) | 4 | none | **Verified 5/5** |
| lambda-sqs-terraform | aws-samples/serverless-patterns | 6 | 2 (customer-managed policy resolved) | 1 | none | **Verified 2/2** |
| eventbridge-lambda-terraform | aws-samples/serverless-patterns | 6 | 2 (1 iam, 1 event) | 3 | none | **Verified 2/2** |

Not run: alex stage 8 and two non-AWS (Azure, GCP) stacks had empty state in
the checkout; one project needed a backend re-init. ITest's detectors are
AWS-only today, so Azure and GCP resources would all report as not analyzed.

## What the AWS samples found

Running ITest on infrastructure it was not built against surfaced five
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

## Resource types ITest does not analyze yet

Seen across the projects above, most frequent first. Every one appears in
`itest plan`'s "Not analyzed" block — nothing is silently skipped.

| Type | Seen in | What it would become |
|---|---|---|
| `aws_api_gateway_*` (REST) and `aws_apigatewayv2_*` | alex 3, alex 7 | route → Lambda integration edge |
| `aws_s3_bucket_notification` | s3-sqs-lambda | S3 → SQS/Lambda event edge |
| `aws_sqs_queue_policy`, `aws_s3_bucket_policy` | s3-sqs-lambda, alex 7 | resource-based grant (the other side of an IAM edge) |
| `aws_cloudwatch_event_rule` / `_target` | eventbridge-lambda | EventBridge rule → target edge |
| `aws_cloudfront_distribution` | alex 7 | CloudFront → origin edge |
| `aws_lambda_function` env vars carrying ARNs/URLs | all | implicit configuration edge |

Types with no integration semantics (log groups, S3 objects, versioning and
encryption settings, packaging helpers such as `null_resource` and
`local_file`) are reported and expected to stay unanalyzed.
