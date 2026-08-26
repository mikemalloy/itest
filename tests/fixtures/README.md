# Test fixtures

## `simple-web-app-plan.json`

A `terraform show -json` representation of [`examples/simple-web-app/`](../../examples/simple-web-app/).
It lets the security-group detector be developed and tested with **no AWS
credentials and no `terraform apply`**.

It encodes exactly one integration chain:

```
internet -> ALB:443 -> web:80 -> db:5432
```

as three security-group relationships:

| Source                      | Target                      | Proto | Ports | Where                                      |
|-----------------------------|-----------------------------|-------|-------|--------------------------------------------|
| `0.0.0.0/0`                 | `aws_security_group.alb`    | tcp   | 443   | inline `ingress` on the ALB SG             |
| `aws_security_group.alb`    | `aws_security_group.web`    | tcp   | 80    | `aws_security_group_rule.web_from_alb`     |
| `aws_security_group.web`    | `aws_security_group.db`     | tcp   | 5432  | `aws_security_group_rule.db_from_web`      |

The file was hand-authored to faithfully match the real schema of
`terraform show -json` output: `planned_values.root_module.resources` with
`aws_security_group` and `aws_security_group_rule` entries. Security-group
ids in `values` are resolved to `sg-…` placeholders (as they are once the SGs
exist), so SG-to-SG references (`source_security_group_id` /
`security_group_id`) can be mapped back to resource addresses without needing
the `configuration` block.

### Regenerating from real Terraform

From `examples/simple-web-app/`:

```sh
terraform init
terraform plan -out=tfplan
terraform show -json tfplan > ../../tests/fixtures/simple-web-app-plan.json
```

`terraform init` and `terraform validate` need no cloud credentials.
`terraform plan` against a real AWS account will refresh state and may render
computed security-group ids as unknown until apply; the checked-in fixture
uses resolved ids so the detector's SG-to-SG mapping is exercised
deterministically in CI.

## `lambda-permission-state.json`

A small synthetic **state** document (`values` root, not `planned_values`)
exercising `aws_lambda_permission` for the event-edge detector. It contains
one Lambda, one S3 bucket, and two permissions: S3 invoking the function
(`source_arn` resolvable to `aws_s3_bucket.uploads`) and API Gateway invoking
it (`source_arn` not present in the document, so it stays an external ARN).

Nothing in it is real: the account ID is the same `111111111111` pseudonym
the alex fixtures use. The alex fixtures themselves carry no
`aws_lambda_permission` resources — stages 4, 7, and 8 of that source project
do, and are candidates for future sanitized fixtures.

## `customer-managed-policy-state.json`

A small synthetic **state** document (`values` root) modelled on AWS's
`lambda-sqs-terraform` serverless pattern, which grants through a
customer-managed policy rather than an inline one. It holds one
`aws_lambda_function`, one `aws_sqs_queue`, one `aws_iam_role`, one
`aws_iam_policy` allowing `sqs:SendMessage` on the queue ARN and `logs:*` on a
wildcard ARN, an `aws_iam_role_policy_attachment` binding the two, and a second
attachment to the AWS-managed `AWSLambdaBasicExecutionRole`.

It exists because the two cases look identical in Terraform and are not:
the AWS-managed policy's document lives in AWS and cannot be read, while the
customer-managed one is right there in the state. The fixture pins that the
first stays an opaque `managed` edge while the second resolves into real
edges carrying `via_policy`.

Nothing in it is real: the account ID is the same `111111111111` pseudonym the
other fixtures use. The alex fixtures carry no `aws_iam_policy` resources,
which is why this shape needed a fixture of its own.

## `alex/`

Sanitized `terraform show -json` **state** output from three stages of a real
production multi-agent RAG system (stage 2: SageMaker embedding endpoint;
stage 5: Aurora + secrets; stage 6: agent Lambdas, SQS, event wiring). They
were passed through `itest redact` (account IDs pseudonymized, Lambda
environment values scrubbed) and are the acceptance targets for the IAM and
event detectors.
