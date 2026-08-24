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
