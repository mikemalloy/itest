# simple-web-app

A minimal three-tier demo stack used throughout ITest's tests and docs. It is
deliberately small — a demo fixture, not production code.

```
internet ──443──▶ ALB ──80──▶ web servers ──5432──▶ Postgres
```

## What it defines

- A VPC with two subnets across two availability zones.
- An **ALB** with a security group allowing `443` from `0.0.0.0/0` (inline
  ingress rule).
- Two `aws_instance` **web servers** with a security group allowing `80` from
  the ALB's security group (SG-to-SG reference, via a standalone
  `aws_security_group_rule`).
- An `aws_db_instance` **Postgres** database with a security group allowing
  `5432` from the web security group (SG-to-SG reference).

Those three security-group rules are exactly the integration chain ITest
detects.

## Validating the Terraform

`init` and `validate` need no AWS credentials:

```sh
terraform init
terraform validate
```

## Regenerating the fixture

The checked-in `terraform show -json` fixture lives at
[`../../tests/fixtures/simple-web-app-plan.json`](../../tests/fixtures/simple-web-app-plan.json).
To regenerate it from real Terraform:

```sh
terraform init
terraform plan -out=tfplan
terraform show -json tfplan > ../../tests/fixtures/simple-web-app-plan.json
```

See [`../../tests/fixtures/README.md`](../../tests/fixtures/README.md) for the
details (including why the checked-in fixture uses resolved security-group ids).

## Running ITest against it

From the repo root, without deploying anything:

```sh
itest plan   --tf-json tests/fixtures/simple-web-app-plan.json
itest sync   --auto-approve --tf-json tests/fixtures/simple-web-app-plan.json
itest verify
```
