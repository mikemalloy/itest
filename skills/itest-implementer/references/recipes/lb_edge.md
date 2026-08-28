# Recipe: `lb_edge`

A load balancer chain, split into two independently checkable hops under one
point type. Which hop a point covers is `attributes.hop`, and **it decides the
whole test** — dispatch on it before anything else.

- `hop: listener` — the load balancer, its listener (or one of that listener's
  rules), and the target group traffic is forwarded to.
- `hop: target_group` — the target group and what actually serves it: an ECS
  service, or individual registered targets.

Fixtures come from the shared template in
[`../conftest.md`](../conftest.md) — write that file once, not per recipe. This
recipe needs its `elbv2` and `ecs` clients.

> **This is the first recipe that asserts liveness, not only wiring.** Every
> other recipe checks that a declaration still matches the deployment. The
> target-group hop goes further: it asks whether anything is actually
> *registered and healthy* behind the group. That is deliberate — an ALB
> forwarding correctly into a group with nothing healthy in it is a working
> configuration serving 503s — but it means a red test here can be a live
> incident rather than drift. Read §6 before classifying one.

## 1. What the fields mean

### Listener hop

```yaml
- id: 255245fbf7d0
  type: lb_edge
  source: module.alb.aws_lb.this[0]
  target: module.alb.aws_lb_target_group.this["ex-ecs"]
  attributes:
    hop: listener
    port: 80
    protocol: HTTP
    rule: priority 1 path=/*
    action: forward
    weight: 100
    external: false
  hcl_address: module.alb.aws_lb_listener_rule.this["ex-http/production"]
```

| Field | Meaning |
|---|---|
| `source` | The `aws_lb` this listener belongs to, as an HCL address. An ARN instead means the load balancer is in another stack. |
| `target` | The `aws_lb_target_group` forwarded to. For a non-forward action it is the action type itself (`redirect`, `fixed-response`) — there is no far end to name. |
| `attributes.port` / `protocol` | The listener's, not the group's. `HTTP`/`HTTPS` on an ALB, `TCP`/`TLS`/`UDP` on an NLB. |
| `attributes.rule` | `default` for the listener's own `default_action`, otherwise `priority <N>` plus the host/path conditions. **The priority is the identity**; the conditions ride along so the summary is readable. |
| `attributes.action` | `forward`, `redirect`, `fixed-response`, `authenticate-*`. Assert it: a forward that became a fixed-response still answers, and answers nothing useful. |
| `attributes.weight` | Present **only** when the forward splits across more than one group. A single-group forward records none, because a weight of 100 on the only group says nothing. |
| `attributes.external` | `true` when the target is not a group in this state: a cross-stack group, or a non-forward action. |
| `hcl_address` | `aws_lb_listener.<name>` for a default action, `aws_lb_listener_rule.<name>` for a rule. |

### Target-group hop

```yaml
- id: 005ecabc1250
  type: lb_edge
  source: module.alb.aws_lb_target_group.this["ex-ecs"]
  target: module.ecs_service.aws_ecs_service.this[0]
  attributes:
    hop: target_group
    via: ecs_service
    container_name: ecsdemo-frontend
    container_port: 3000
    health_check_path: /
    target_type: ip
    deployment_role: production
    cluster: module.ecs_cluster.aws_ecs_cluster.this[0]
    task_definition: module.ecs_service.aws_ecs_task_definition.this[0]
    external: false
  hcl_address: module.ecs_service.aws_ecs_service.this[0]
```

| Field | Meaning |
|---|---|
| `source` | The `aws_lb_target_group`, always an HCL address in this state. |
| `target` | What feeds it: an `aws_ecs_service` address, a registered instance/function address, a bare id or IP that nothing in state owns, or `(empty)`. |
| `attributes.via` | `ecs_service`, `attachment`, or `none`. Second-level dispatch, after `hop`. |
| `attributes.container_name` / `container_port` | The container the service registers into the group (`via: ecs_service`). |
| `attributes.port` | The registered port (`via: attachment`). |
| `attributes.health_check_path` | The group's HTTP health check path, or null for a TCP check. |
| `attributes.target_type` | `ip`, `instance`, `lambda`, or `alb`. It decides what a registered target id looks like. |
| `attributes.deployment_role` | `production` or `alternate`. `alternate` is the standby group of a blue/green deployment — see §5. |
| `attributes.cluster` / `task_definition` | Context resolved from the service. Use `cluster` for the `describe_services` call; never re-derive it. |
| `hcl_address` | The resource that declares the feed: the ECS service, the attachment, or the group itself when nothing feeds it. |

## 2. What to assert, and what not to

The honest checks are **"does this listener still forward there, on that port"**
and **"is anything registered and healthy behind that group"** — both read back
from AWS.

**Do not send a request to the load balancer.** Curling the DNS name runs
whatever is behind it, which for a POST route may write. That is an
active probe: it belongs to the active tier the interview enables explicitly,
and it is not what these points claim. Every call below is a `describe_*`.

**Do not register, deregister, or set a target's health.** `elbv2` has write
calls that read like diagnostics; none of them belong here.

## 3. Listener hop

`hcl_address` gives you the listener or rule; `resolve` turns it into live
values carrying the ARNs. Read the listener back for port and protocol, then
the action for where it forwards.

```python
def test_lb_module_alb_aws_lb_this_0_HTTP_80_to_ex_ecs_priority_1_path(elbv2, resolve):
    """Integration point 255245fbf7d0.

    module.alb.aws_lb.this[0] -> module.alb.aws_lb_target_group.this["ex-ecs"]
    type=lb_edge action=forward external=False hop=listener port=80
      protocol=HTTP rule=priority 1 path=/* weight=100
    HCL: module.alb.aws_lb_listener_rule.this["ex-http/production"]
    """
    rule = resolve('module.alb.aws_lb_listener_rule.this["ex-http/production"]')
    group = resolve('module.alb.aws_lb_target_group.this["ex-ecs"]')

    listeners = elbv2.describe_listeners(ListenerArns=[rule["listener_arn"]])
    listener = listeners["Listeners"][0]
    assert (listener["Port"], listener["Protocol"]) == (80, "HTTP"), (
        f"Listener is now {listener['Protocol']}:{listener['Port']}, not "
        "HTTP:80. Traffic this point describes arrives somewhere else."
    )

    live = next(
        r
        for r in elbv2.describe_rules(ListenerArn=rule["listener_arn"])["Rules"]
        if r["RuleArn"] == rule["arn"]
    )
    assert forward_weight(live, group["arn"]) == 100, (
        f"Rule priority {live['Priority']} no longer forwards weight 100 to "
        f"{group['arn']}. Forward targets: {forward_targets(live)}"
    )
```

For a **default action**, the point's `rule` is `default` and there is no rule
resource: `hcl_address` is the listener itself, so read its `DefaultActions`
from `describe_listeners` and skip `describe_rules` entirely.

```python
def test_lb_module_alb_aws_lb_this_0_HTTP_80_to_fixed_response_default(elbv2, resolve):
    """Integration point afb240b7d26f.

    module.alb.aws_lb.this[0] -> fixed-response
    type=lb_edge action=fixed-response external=True hop=listener port=80
      protocol=HTTP rule=default
    HCL: module.alb.aws_lb_listener.this["ex-http"]
    """
    listener_values = resolve('module.alb.aws_lb_listener.this["ex-http"]')

    listener = elbv2.describe_listeners(ListenerArns=[listener_values["arn"]])[
        "Listeners"
    ][0]
    types = [action["Type"] for action in listener["DefaultActions"]]
    assert "fixed-response" in types, (
        f"The listener's default action is now {types}, not fixed-response. "
        "Requests matching no rule are answered differently."
    )
```

Two helpers keep the forward matching in one place, because AWS returns a
forward two different ways (§4):

```python
def forward_targets(action_or_rule):
    """Every (target group arn, weight) a forward action names.

    `Actions[].ForwardConfig.TargetGroups` is the general form; a single-group
    forward *also* carries a bare `Actions[].TargetGroupArn`, and older
    listeners carry only the bare form. Read both or a correct forward reads
    as missing.
    """
    actions = action_or_rule.get("Actions", [action_or_rule])
    found = []
    for action in actions:
        if action.get("Type") != "forward":
            continue
        config = action.get("ForwardConfig") or {}
        for group in config.get("TargetGroups") or []:
            found.append((group["TargetGroupArn"], group.get("Weight")))
        if not config.get("TargetGroups") and action.get("TargetGroupArn"):
            found.append((action["TargetGroupArn"], None))
    return found


def forward_weight(action_or_rule, target_group_arn):
    """The weight forwarded to one group, or None when it is not a target."""
    for arn, weight in forward_targets(action_or_rule):
        if arn == target_group_arn:
            return weight
    return None
```

When the point has **no `weight`**, assert membership rather than a number —
the forward names one group, and its weight is not part of the claim:

```python
def assert_forwards_to(rule, target_group_arn, point_id):
    """Membership assertion for an unweighted forward."""
    arns = [arn for arn, _ in forward_targets(rule)]
    assert target_group_arn in arns, (
        f"Integration point {point_id}: the rule no longer forwards to "
        f"{target_group_arn}. It forwards to {arns}."
    )
```

**`external: true` on this hop** means one of two things, and they are checked
differently. A `redirect` or `fixed-response` target is the action *type*:
assert that type is still what the listener or rule does, and stop — there is
no far end to follow. A cross-stack target group ARN is a real ARN the manifest
holds: read it with the `point` fixture (never paste it) and assert the forward
still names it.

## 4. API constraints worth knowing before you write the call

These are the elbv2/ECS equivalents of the ARN-shape trap documented in
[`iam_edge.md`](iam_edge.md) §"Notes on matching": each one makes a correct
deployment fail a naively written assertion.

- **A forward has two spellings.** `ForwardConfig.TargetGroups[]` and the bare
  `Actions[].TargetGroupArn`. AWS returns whichever the listener was created
  with, and sometimes both. Match against both — that is what
  `forward_targets` above is for.
- **`Priority` comes back as a string.** `describe_rules` returns `"1"`, and
  `"default"` for the default rule, never an int. Compare as strings, or
  compare `RuleArn` as the examples above do.
- **`describe_rules` includes the default rule.** It appears with
  `IsDefault: true` and `Priority: "default"`. Filtering by `RuleArn` avoids
  ever having to special-case it.
- **`describe_listeners` takes one selector.** Either `LoadBalancerArn` or
  `ListenerArns` — passing both is an error. Both paginate; use a paginator if
  a listener has many rules.
- **An empty group is not an error.** `describe_target_health` on a group with
  nothing registered returns `TargetHealthDescriptions: []` and a 200. The
  emptiness is the finding, so assert on the list — do not expect an exception.
- **`describe_services` reports failures instead of raising.** A service that
  is gone comes back as an empty `services` list plus an entry in `failures`.
  Check `services` explicitly, or a deleted service reads as an `IndexError`
  with no explanation.
- **ECS is camelCase, elbv2 is PascalCase.** `service["loadBalancers"][0]
  ["targetGroupArn"]` next to `group["TargetGroupArn"]` in the same test is
  correct, not a typo.

## 5. Target-group hop

Dispatch on `via`.

### 5a. `via: ecs_service`

Three claims, one test: the group's health check is still what the point
records, the service is still registered into that group on that container and
port, and something is actually healthy behind it.

```python
def test_lb_ex_ecs_to_ecs_service_this_0_ecs_service_3000(elbv2, ecs, resolve):
    """Integration point 005ecabc1250.

    module.alb.aws_lb_target_group.this["ex-ecs"] -> module.ecs_service.aws_ecs_service.this[0]
    type=lb_edge cluster=module.ecs_cluster.aws_ecs_cluster.this[0]
      container_name=ecsdemo-frontend container_port=3000
      deployment_role=production external=False health_check_path=/
      hop=target_group target_type=ip
      task_definition=module.ecs_service.aws_ecs_task_definition.this[0]
      via=ecs_service
    HCL: module.ecs_service.aws_ecs_service.this[0]
    """
    group = resolve('module.alb.aws_lb_target_group.this["ex-ecs"]')
    service_values = resolve("module.ecs_service.aws_ecs_service.this[0]")
    cluster = resolve("module.ecs_cluster.aws_ecs_cluster.this[0]")

    live_group = elbv2.describe_target_groups(TargetGroupArns=[group["arn"]])[
        "TargetGroups"
    ][0]
    assert live_group["HealthCheckPath"] == "/", (
        f"Health check path is {live_group['HealthCheckPath']!r}, not '/'. "
        "The group is grading its targets on a different endpoint than this "
        "point records."
    )

    described = ecs.describe_services(
        cluster=cluster["arn"], services=[service_values["arn"]]
    )
    assert described["services"], (
        f"ECS service {service_values['arn']} is not in cluster "
        f"{cluster['arn']}: {described['failures']}"
    )
    service = described["services"][0]

    registered = [
        lb for lb in service["loadBalancers"] if lb["targetGroupArn"] == group["arn"]
    ]
    assert registered, (
        f"The service no longer registers into {group['arn']}. It registers "
        f"into {[lb['targetGroupArn'] for lb in service['loadBalancers']]}."
    )
    assert (
        registered[0]["containerName"],
        registered[0]["containerPort"],
    ) == ("ecsdemo-frontend", 3000), (
        f"The service registers {registered[0]['containerName']}:"
        f"{registered[0]['containerPort']} into the group, not "
        "ecsdemo-frontend:3000."
    )

    health = elbv2.describe_target_health(TargetGroupArn=group["arn"])
    states = [t["TargetHealth"]["State"] for t in health["TargetHealthDescriptions"]]
    assert states, (
        f"Nothing is registered in {group['arn']}. The listener forwards "
        f"there and the group is empty; the service reports "
        f"{service['runningCount']}/{service['desiredCount']} tasks running."
    )
    assert states.count("healthy") >= 1, (
        f"No healthy target in {group['arn']} (states: {states}). The service "
        f"reports {service['runningCount']} running of {service['desiredCount']} "
        f"desired, status {service['status']}."
    )
```

`desiredCount` and `runningCount` belong in the failure message, not in an
assertion of their own: they are what turns "no healthy targets" into a
diagnosis. `0/0` is a service scaled to zero; `0/2` is a service whose tasks
are failing to start; `2/2` with no healthy target is a health check failing
against running tasks.

### 5b. `via: attachment`

Same health assertions, but the registration is an attachment rather than a
service, so match the resolved target's id and port against what
`describe_target_health` reports.

```python
def test_lb_web_group_to_web_attachment_8080(elbv2, resolve):
    """Integration point <id>.

    aws_lb_target_group.web -> aws_instance.web
    type=lb_edge external=False health_check_path=/healthz hop=target_group
      port=8080 target_type=instance via=attachment
    HCL: aws_lb_target_group_attachment.web
    """
    group = resolve("aws_lb_target_group.web")
    instance = resolve("aws_instance.web")

    health = elbv2.describe_target_health(TargetGroupArn=group["arn"])
    described = {
        (t["Target"]["Id"], t["Target"].get("Port")): t["TargetHealth"]["State"]
        for t in health["TargetHealthDescriptions"]
    }
    state = described.get((instance["id"], 8080))
    assert state is not None, (
        f"{instance['id']}:8080 is not registered in {group['arn']}. "
        f"Registered: {sorted(described)}"
    )
    assert state == "healthy", f"{instance['id']}:8080 is {state} in {group['arn']}."
```

For `target_type: lambda` the registered `Target.Id` is the function ARN and
there is no port; drop the port from the key. When `external: true` the target
id is an IP or an id nothing in this state owns — read it from the manifest
with `point(...)["target"]` instead of `resolve`, and never paste it.

### 5c. `via: none` — the empty group

The point's target is `(empty)`, which is not an address and must never be
resolved. The claim is the finding itself: as of the last plan, nothing in the
Terraform declared a feed for this group.

Assert what is true live, so the test tracks reality rather than the plan:

```python
def test_lb_orphan_group_to_empty_none(elbv2, resolve):
    """Integration point <id>.

    aws_lb_target_group.orphan -> (empty)
    type=lb_edge external=True health_check_path=/healthz hop=target_group
      target_type=ip via=none
    HCL: aws_lb_target_group.orphan
    """
    group = resolve("aws_lb_target_group.orphan")

    health = elbv2.describe_target_health(TargetGroupArn=group["arn"])
    registered = health["TargetHealthDescriptions"]
    assert not registered, (
        f"{group['arn']} is recorded as fed by nothing, but AWS reports "
        f"{len(registered)} registered target(s). Something outside this "
        "Terraform is registering into it."
    )
```

That assertion is deliberately the opposite way round from the others: it
fails when the *deployment* disagrees with the manifest, which for an empty
group means an out-of-band registration. Raise the emptiness itself with the
user as a finding when you generate the test — do not leave it only in a
docstring.

### 5d. `deployment_role: alternate`

The standby group of a blue/green deployment. It is fed by the same service,
through `advanced_configuration.alternate_target_group_arn`, and **between
deployments it is legitimately empty or unhealthy** — that is what standby
means.

So: assert the registration (the service still names it as its alternate) and
the health check path, but **do not assert a healthy count** unless the user
confirms the deployment strategy keeps both sides warm. Say in the failure
message which side the point covers, so nobody reads a standby group's
emptiness as an outage.

### 5e. Blue/green pairs -- the weights and the live side are runtime-owned

`deployment_role` marks a point as one half of a pair. When the service's live
`deploymentConfiguration.strategy` is `BLUE_GREEN` (or `LINEAR` / `CANARY`),
**ECS owns two things the manifest froze at apply time**:

- the listener rule's forward **weights**, which it rewrites on every deploy, and
- **which group holds tasks** -- after a flip the group Terraform called
  `production` is legitimately empty and the `alternate` holds the healthy task.

So a point that pins `weight: 100` to a named group, or asserts healthy targets
behind the `production` group specifically, goes red on a completely healthy
service the first time anyone deploys. That is neither drift nor a bad
deployment: it is the assertion claiming ownership of a value it does not own.

Assert what Terraform actually owns, and assert liveness **across the pair**:

| Claim | Owner | Assert? |
|---|---|---|
| the rule forwards to this group | Terraform | yes |
| total forward weight is 100, with a live side | ECS, but invariant | yes |
| *which* side carries the weight | ECS, flips per deploy | **no** |
| each group's health check path | Terraform | yes |
| container name and port registered | Terraform | yes |
| *which* side holds healthy targets | ECS, flips per deploy | **no** |
| at least one side of the pair is healthy | -- | yes |

Dispatch on the **live** strategy, never on the manifest -- the manifest cannot
see a strategy that changed after the last apply:

```python
def is_blue_green(service):
    """True when ECS, not Terraform, owns this service's weights and live side."""
    strategy = (service.get("deploymentConfiguration") or {}).get("strategy")
    return strategy in {"BLUE_GREEN", "LINEAR", "CANARY"}
```

#### Listener hop on a blue/green pair

The pair is the rule's own forward set, so this hop needs no ECS call. Assert
membership and the weight *invariant*, never the split:

```python
    live = live_rule(elbv2, rule)
    targets = dict(forward_targets(live))
    assert group["arn"] in targets, (
        f"Rule priority {live['Priority']} no longer forwards to "
        f"{group['arn']}. Forward targets: {sorted(targets)}"
    )
    total = sum(weight or 0 for weight in targets.values())
    assert total == 100, (
        f"Forward weights across the pair sum to {total}, not 100: {targets}"
    )
    assert any(targets.values()), (
        f"Every group in the pair carries weight 0, so the rule forwards "
        f"nowhere: {targets}"
    )
```

Which member carries the weight is deliberately unasserted. Say so in the
docstring and name the weight the point recorded, so a reader can see what the
manifest froze and why it is not being enforced.

#### Target-group hop on a blue/green pair

Keep the health check path and the registration assertions from §5a and §5d --
both are Terraform's. Replace only the per-group healthy assertion with a
pair-level one, so the check still proves something is serving:

```python
    this_side = target_states(elbv2, group["arn"])
    other_side = target_states(elbv2, partner_arn)
    assert this_side.count("healthy") + other_side.count("healthy") >= 1, (
        f"Neither side of the blue/green pair has a healthy target. "
        f"{group['arn']}: {this_side}; {partner_arn}: {other_side}. The "
        f"service reports {service['runningCount']} running of "
        f"{service['desiredCount']} desired, status {service['status']}."
    )
```

The partner ARN comes from the service, not from a second `resolve`: the
production side is `loadBalancers[].targetGroupArn` and the alternate is
`loadBalancers[].advancedConfiguration.alternateTargetGroupArn`, so a single
`describe_services` yields both halves whichever side the point covers.

A `ROLLING` service keeps §5a exactly as written -- one group, one healthy
assertion. Do not apply this section to it, and branch on `is_blue_green` rather
than assuming, so the test stays correct if the strategy is changed later.

## 6. What a failure means

Classify before reporting. This recipe produces three different kinds of red,
and only the first is ordinary drift.

1. **Wiring drift.** A listener port or protocol that moved, a forward that no
   longer names the group, a weight that changed, a health check path that
   changed, a service that registers a different container or port. The
   Terraform and the deployment disagree: report it as drift, naming both ends
   and both values.
2. **No healthy targets** (`states.count("healthy") == 0`). The wiring is
   correct and the thing behind it is not serving. **This is a finding either
   way, and it is not a bad assertion.** `0/0` running means the service is
   scaled to zero — intended in a paused environment, an outage in production,
   and only the user knows which. `0/N` running means tasks are failing to
   start (image, IAM, subnet, or a crash loop). `N/N` running with nothing
   healthy means the health check is failing against live tasks — often the
   `health_check_path` returning non-2xx. Report the state list, the
   running/desired counts, and ask the user which of those it is. Do not
   weaken the assertion to "the group exists" to get green.
3. **Nothing registered at all** (`TargetHealthDescriptions == []` on a group
   the listener forwards to). The ALB is routing into a void and serving 503s.
   Say exactly that. **Check the forward weight before you do.** On a blue/green
   pair the drained side legitimately holds nothing while carrying weight 0 --
   the listener is not sending traffic there at all, so it is not a void. It is
   only case 3 when the group actually receives traffic; otherwise it is §5e.

`LookupError` from `resolve` means the address is not in the current state —
renamed, removed, or the wrong `terraform_dir`.

`TargetGroupNotFoundException` / `ListenerNotFoundException` means the resource
is gone from AWS while the manifest still carries it: drift, not a test bug.

An `AccessDenied` on `elasticloadbalancing:Describe*` or `ecs:DescribeServices`
is about the *caller*, not the deployment. Say so and ask for the read
permission rather than reporting a finding.

**Existence is not reachability.** A green run here proves the listener
forwards to the group, the group's health check is what was declared, and at
least one target is passing it. Whether a client reaches the load balancer
depends on security groups, DNS, and WAF — separate points with their own
detectors. Do not claim more than the check covers, and never widen a target
list, drop the health assertion, or lower `>= 1` to `>= 0` to make a red test
pass.
