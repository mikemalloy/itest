# Recipe: `sg_edge`

A security-group edge. The point asserts "source can reach target on this
protocol and port range", as declared by a security group rule.

## 1. What the fields mean

A `sg_edge` point in `.itest/manifest.yaml` looks like this:

```yaml
- id: ddd86d182890
  type: sg_edge
  source: aws_security_group.web
  target: aws_security_group.db
  attributes:
    protocol: tcp
    ports: '5432'
    direction: ingress
  hcl_address: aws_security_group_rule.db_from_web
```

| Field | Meaning |
|---|---|
| `source` | Where traffic comes from. Either an **HCL address** of a security group (`aws_security_group.web`) or a raw **CIDR block** (`0.0.0.0/0`). These two cases assert differently — see §3. |
| `target` | The security group the rule is attached to. Always an HCL address. |
| `attributes.protocol` | `tcp`, `udp`, `icmp`, or `-1` for all. Matches `IpProtocol` in the EC2 API. |
| `attributes.ports` | A single port (`443`) or a range (`8000-8080`). Maps to `FromPort`/`ToPort`. |
| `attributes.direction` | `ingress` or `egress`. Decides whether you read `IpPermissions` or `IpPermissionsEgress`. |
| `hcl_address` | Where the rule is declared. Either a standalone `aws_security_group_rule.<name>`, or an inline block like `aws_security_group.alb.ingress[0]`. Used to resolve live ids. |

Note the detector's asymmetry: ingress rules always produce an edge, egress
rules only when they target another security group. So most points you see
will be `direction: ingress`.

## 2. The resolver

Terraform HCL addresses are not AWS ids. The manifest says
`aws_security_group.db`; the API needs `sg-0a1b2c3d`. Hardcoding the id into a
test breaks on the next `terraform destroy`/`apply` cycle and makes the test
environment-specific.

So resolve at run time. The shared `conftest.py` template in
[`../conftest.md`](../conftest.md) runs `terraform show -json` once per session
and exposes `resolve` plus the read-only service clients. Write that file on
first generation — it is shared by every recipe, so do not create a second copy
here.

## 3. The assertion pattern

Shape, in every case:

1. Resolve the **target** security group to its live `id`.
2. `ec2.describe_security_groups(GroupIds=[target_id])` — a read-only call.
3. Read `IpPermissions` for `direction: ingress`, `IpPermissionsEgress` for
   `direction: egress`.
4. Assert at least one permission matches the protocol, the port range, **and**
   the source.
5. Put the point's identity in the assertion message, so a failure reads as a
   finding rather than a stack trace.

Matching the source is where the two kinds diverge.

### 3a. CIDR source

When `source` is a CIDR block, match against the permission's `IpRanges`.

```python
def test_sg_internet_to_alb_443(ec2, resolve):
    """Integration point 7a05510a0b6c.

    0.0.0.0/0 -> aws_security_group.alb
    protocol=tcp ports=443 direction=ingress
    HCL: aws_security_group.alb.ingress[0]
    """
    target = resolve("aws_security_group.alb")
    groups = ec2.describe_security_groups(GroupIds=[target["id"]])
    permissions = groups["SecurityGroups"][0]["IpPermissions"]

    matches = [
        p
        for p in permissions
        if p.get("IpProtocol") == "tcp"
        and p.get("FromPort") == 443
        and p.get("ToPort") == 443
        and any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", []))
    ]
    assert matches, (
        f"aws_security_group.alb ({target['id']}) has no ingress rule "
        "allowing tcp:443 from 0.0.0.0/0. The deployed security group does "
        "not match integration point 7a05510a0b6c."
    )
```

### 3b. Security-group source

When `source` is an HCL address, resolve it too and match against the
permission's `UserIdGroupPairs`.

```python
def test_sg_web_to_db_5432(ec2, resolve):
    """Integration point ddd86d182890.

    aws_security_group.web -> aws_security_group.db
    protocol=tcp ports=5432 direction=ingress
    HCL: aws_security_group_rule.db_from_web
    """
    source = resolve("aws_security_group.web")
    target = resolve("aws_security_group.db")
    groups = ec2.describe_security_groups(GroupIds=[target["id"]])
    permissions = groups["SecurityGroups"][0]["IpPermissions"]

    matches = [
        p
        for p in permissions
        if p.get("IpProtocol") == "tcp"
        and p.get("FromPort") == 5432
        and p.get("ToPort") == 5432
        and any(
            pair.get("GroupId") == source["id"]
            for pair in p.get("UserIdGroupPairs", [])
        )
    ]
    assert matches, (
        f"aws_security_group.db ({target['id']}) has no ingress rule allowing "
        f"tcp:5432 from aws_security_group.web ({source['id']}). The deployed "
        "security group does not match integration point ddd86d182890."
    )
```

### Notes on matching

- **Port ranges.** `ports: '8000-8080'` means `FromPort == 8000` and
  `ToPort == 8080`. Split on `-`; a single port sets both to the same value.
- **Protocol `-1`.** Means "all traffic". AWS reports `IpProtocol == "-1"` with
  no `FromPort`/`ToPort`. Match the protocol alone and skip the port check.
- **Egress.** Read `IpPermissionsEgress` instead. Everything else is identical.
- **A rule may be broader than the point.** A permission for `0.0.0.0/0`
  satisfies a point sourced from a narrower CIDR. Matching exactly, as above,
  is the strict reading and the right default: it tests what Terraform
  declared. Only loosen it if the user asks.

## 4. What a failure means

A red test here is **not** automatically a bug in the test.

`assert matches` failing means: the security group exists in the live account,
but no rule on it grants the access the integration point describes. The
Terraform says A can reach B; the account says otherwise. That is drift, and
finding it is the entire point of the tool.

`LookupError` from `resolve_address` means the address in the manifest is not
in the current Terraform state at all — the resource was renamed or removed,
or the configured `terraform_dir` points somewhere else.

Report either as a **finding**, with the resolved ids and the rule you expected
to see. Ask the user whether it is drift or a bad assertion. Do not relax the
comparison to make it pass — a test that asserts nothing costs more than no
test, because it looks like coverage.
