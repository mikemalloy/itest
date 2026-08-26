# Recipe: `iam_edge`

An IAM grant. The point asserts "this role is allowed these actions on that
resource", as declared by a policy statement.

Fixtures come from the shared template in
[`../conftest.md`](../conftest.md) — write that file once, not per recipe.

## 1. What the fields mean

An `iam_edge` point in `.itest/manifest.yaml` looks like this:

```yaml
- id: 5e8e502d62b7
  type: iam_edge
  source: aws_iam_role.lambda_aurora_role
  target: aws_rds_cluster.aurora
  attributes:
    actions:
      - rds-data:ExecuteStatement
      - rds-data:BeginTransaction
    effect: Allow
    wildcard_action: false
    wildcard_resource: false
    external: false
    managed: false
  hcl_address: aws_iam_role_policy.lambda_aurora_policy
```

| Field | Meaning |
|---|---|
| `source` | The IAM role's HCL address. Always a role, never a user or group. |
| `target` | What the statement grants access to. **Either** an HCL address (the ARN matched a resource in the same state) **or** a raw ARN (it did not — see `external`). |
| `attributes.actions` | Every action in the statement, sorted. One edge covers one (role, resource) pair; actions ride here rather than fanning out into an edge each. |
| `attributes.effect` | `Allow` or `Deny`. Deny statements are emitted too, and invert the assertion — see §3d. |
| `attributes.wildcard_action` | An action contains `*` (`s3:*`). The grant is broader than it looks. |
| `attributes.wildcard_resource` | The resource ARN contains `*`. The grant spans resources that may not exist yet. |
| `attributes.external` | The ARN did not resolve to anything in this state. Usually a cross-stack reference: another stack owns the resource. |
| `attributes.managed` | The edge came from a policy *attachment*, not an inline statement. `actions` is `["<unresolved>"]` because the policy document lives in AWS. |
| `hcl_address` | Where the grant is declared: `aws_iam_role.<name>` for an inline policy, `aws_iam_role_policy.<name>` standalone, `aws_iam_role_policy_attachment.<name>` for a managed attachment. |

## 2. What to assert, and what not to

The honest check for an IAM edge is **"does the deployed policy still grant
this?"** — not "can this role reach that resource?", which depends on resource
policies, SCPs, permission boundaries, and VPC endpoints you cannot see from
here.

Two read-only approaches, in order of preference:

1. **`iam:SimulatePrincipalPolicy`** — AWS evaluates the whole policy chain and
   answers `allowed` or `implicitDeny`. This is the closest thing to truth, and
   it is read-only despite the name: it simulates, it does not act.
2. **Read the policy document** and assert the statement is present. Cheaper,
   no simulation permission needed, but blind to anything layered on top.

Prefer (1). Fall back to (2) when the caller lacks `iam:SimulatePrincipalPolicy`,
and say so in the test's docstring so nobody mistakes its scope.

## 3. The assertion pattern

### 3a. Resolved target (`external: false`)

The target is an HCL address, so resolve it to a live ARN first.

```python
def test_iam_lambda_aurora_role_to_aurora(iam, resolve):
    """Integration point 5e8e502d62b7.

    aws_iam_role.lambda_aurora_role -> aws_rds_cluster.aurora
    type=iam_edge actions=rds-data (5 actions) effect=Allow
    HCL: aws_iam_role_policy.lambda_aurora_policy
    """
    role = resolve("aws_iam_role.lambda_aurora_role")
    cluster = resolve("aws_rds_cluster.aurora")
    actions = [
        "rds-data:ExecuteStatement",
        "rds-data:BeginTransaction",
    ]

    result = iam.simulate_principal_policy(
        PolicySourceArn=role["arn"],
        ActionNames=actions,
        ResourceArns=[cluster["arn"]],
    )

    denied = [
        r["EvalActionName"]
        for r in result["EvaluationResults"]
        if r["EvalDecision"] != "allowed"
    ]
    assert not denied, (
        f"aws_iam_role.lambda_aurora_role ({role['arn']}) is denied {denied} "
        f"on {cluster['arn']}. The deployed policy no longer grants what "
        "integration point 5e8e502d62b7 describes."
    )
```

### 3b. External target (`external: true`)

The target is already an ARN, so there is nothing to resolve — use it directly.
Say in the docstring that it is cross-stack, because a failure here often means
*the other stack* changed, not this one.

```python
def test_iam_lambda_agents_role_to_sagemaker_endpoint(iam, resolve):
    """Integration point ef585ce57133.

    aws_iam_role.lambda_agents_role -> arn:aws:sagemaker:…:endpoint/alex-embedding-endpoint
    type=iam_edge actions=sagemaker:InvokeEndpoint effect=Allow external=True
    HCL: aws_iam_role_policy.lambda_agents_policy

    Cross-stack: the endpoint is owned by another stack. A failure may mean it
    was renamed or removed there.
    """
    role = resolve("aws_iam_role.lambda_agents_role")
    target_arn = (
        "arn:aws:sagemaker:us-west-1:111111111111:endpoint/alex-embedding-endpoint"
    )

    result = iam.simulate_principal_policy(
        PolicySourceArn=role["arn"],
        ActionNames=["sagemaker:InvokeEndpoint"],
        ResourceArns=[target_arn],
    )
    decision = result["EvaluationResults"][0]["EvalDecision"]
    assert decision == "allowed", (
        f"aws_iam_role.lambda_agents_role is {decision} on "
        f"sagemaker:InvokeEndpoint for {target_arn} (cross-stack)."
    )
```

### 3c. Managed policy (`managed: true`)

`actions` is `["<unresolved>"]`, so there is nothing to simulate. Assert the
attachment still exists — that is the whole claim the point makes.

```python
def test_iam_lambda_agents_role_managed_basic_execution(iam, resolve):
    """Integration point a54b0e0e3663.

    aws_iam_role.lambda_agents_role -> arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    type=iam_edge managed=True
    HCL: aws_iam_role_policy_attachment.lambda_agents_basic
    """
    role = resolve("aws_iam_role.lambda_agents_role")
    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

    attached = iam.list_attached_role_policies(RoleName=role["name"])
    arns = [p["PolicyArn"] for p in attached["AttachedPolicies"]]
    assert policy_arn in arns, (
        f"{policy_arn} is no longer attached to {role['name']}. Attached: {arns}"
    )
```

When `broad_managed_policy` is also true, the policy is an AWS-managed
`*FullAccess`. Keep the existence assertion, and add a docstring line saying the
grant is wider than the point implies, so a reviewer sees it.

### 3d. Deny statements (`effect: "Deny"`)

Invert the assertion. A Deny edge claims access is **blocked**, and a test that
passes when it is allowed has it exactly backwards.

```python
def test_iam_batch_role_denied_prod_bucket(iam, resolve):
    """Integration point <id>.

    aws_iam_role.batch -> arn:aws:s3:::prod-exports/*
    type=iam_edge actions=s3:DeleteObject effect=Deny
    HCL: aws_iam_role_policy.batch_guardrails
    """
    role = resolve("aws_iam_role.batch")

    result = iam.simulate_principal_policy(
        PolicySourceArn=role["arn"],
        ActionNames=["s3:DeleteObject"],
        ResourceArns=["arn:aws:s3:::prod-exports/report.csv"],
    )

    denied = [r for r in result["EvaluationResults"] if r["EvalDecision"] != "allowed"]
    assert denied, (
        "Expected the Deny statement to block s3:DeleteObject on "
        "prod-exports, but it is allowed. The deny was removed or is being "
        "overridden."
    )
```

### Notes on matching

- **`wildcard_resource: true`.** The point's target ends in `*`. Simulate
  against a concrete ARN that the wildcard should cover, not the literal
  wildcard string — AWS evaluates a literal `*` as a resource name.
- **`wildcard_action: true`.** Simulate the specific actions the code relies on
  rather than the `*` itself, and note in the docstring that the grant is
  broader than what is tested.
- **Simulation is not reachability.** A passing simulation means the policy
  allows it. Network path, resource policies, and endpoint availability are
  separate concerns with their own detectors — do not claim more than the check
  covers.

## 4. What a failure means

`EvalDecision != "allowed"` means the role no longer has the grant the
Terraform declared. That is drift, and finding it is the point.

`implicitDeny` specifically means nothing grants the action — usually a policy
was narrowed or a statement dropped. `explicitDeny` means something actively
blocks it: a boundary, an SCP, or a Deny statement added since.

`LookupError` from `resolve` means the address in the manifest is not in the
current state at all — renamed, removed, or the wrong `terraform_dir`.

An `AccessDenied` error on `simulate_principal_policy` itself is neither: it
means the *caller* lacks simulation permission. Fall back to §2's approach (2)
and say so, rather than reporting a finding that is really about your own
credentials.

Report a real failure as a **finding**, with the role ARN, the target ARN, and
the actions that were refused. Ask the user whether it is drift or a bad
assertion. Never widen the action list or drop a resource to make it pass.
