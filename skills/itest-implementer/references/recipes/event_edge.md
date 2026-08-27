# Recipe: `event_edge`

An invocation path. The point asserts "this source triggers that target", as
declared by an event source mapping, a redrive policy, a Lambda permission, an
S3 bucket notification, or an EventBridge rule target.

Fixtures come from the shared template in
[`../conftest.md`](../conftest.md) — write that file once, not per recipe.

## 1. What the fields mean

An `event_edge` point in `.itest/manifest.yaml` looks like this:

```yaml
- id: d22d32578df2
  type: event_edge
  source: aws_sqs_queue.analysis_jobs
  target: aws_lambda_function.planner
  attributes:
    mechanism: event_source_mapping
    batch_size: 1
    enabled: true
    external: false
  hcl_address: aws_lambda_event_source_mapping.planner_sqs
```

| Field | Meaning |
|---|---|
| `source` | Where the invocation comes from: a queue or stream (ESM), the queue that overflows (DLQ redrive), or a principal / source ARN (Lambda permission). |
| `target` | What gets invoked: the function, or the dead-letter queue. HCL address when it resolved, raw ARN otherwise. |
| `attributes.mechanism` | `event_source_mapping`, `dlq_redrive`, `lambda_permission`, `s3_notification`, or `eventbridge_target`. **This decides which assertion to write** — they are not interchangeable. |
| `attributes.external` | The ARN did not resolve within this state; another stack owns it. |
| `attributes.batch_size`, `enabled` | ESM only. A disabled mapping is wired but not delivering, which is worth asserting explicitly. |
| `attributes.max_receive_count` | DLQ redrive only. How many failures before a message moves to the DLQ. |
| `attributes.action`, `principal` | Lambda permission only, e.g. `lambda:InvokeFunction` and `s3.amazonaws.com`. |
| `attributes.events`, `filter_prefix`, `filter_suffix` | S3 notification only. Which object events fire, and the key filter that narrows them. |
| `attributes.event_bus_name`, `trigger`, `enabled` | EventBridge target only. The bus the rule lives on, `schedule` or `pattern`, and whether the rule is enabled. |
| `hcl_address` | The resource declaring the wiring. |

Dispatch on `mechanism` first. Everything below is per-mechanism.

## 2. `mechanism: event_source_mapping`

Assert the mapping still exists, still points at both ends, and is enabled.
`list_event_source_mappings` is read-only.

```python
def test_event_analysis_jobs_to_planner(lambda_, resolve):
    """Integration point d22d32578df2.

    aws_sqs_queue.analysis_jobs -> aws_lambda_function.planner
    type=event_edge mechanism=event_source_mapping batch_size=1 enabled=True
    HCL: aws_lambda_event_source_mapping.planner_sqs
    """
    queue = resolve("aws_sqs_queue.analysis_jobs")
    function = resolve("aws_lambda_function.planner")

    mappings = lambda_.list_event_source_mappings(
        FunctionName=function["function_name"],
        EventSourceArn=queue["arn"],
    )["EventSourceMappings"]

    assert mappings, (
        f"No event source mapping from {queue['arn']} to "
        f"{function['function_name']}. The wiring integration point "
        "d22d32578df2 describes is gone."
    )
    states = [m["State"] for m in mappings]
    assert any(s == "Enabled" for s in states), (
        f"The mapping exists but is not enabled (states: {states}). Messages "
        "are not being delivered."
    )
```

`State` is the live truth and can be `Enabled`, `Disabled`, `Creating`,
`Updating`, or `Deleting`. Assert on `Enabled` only when the point's `enabled`
attribute is true; when it is false the mapping is *deliberately* off and the
assertion should say so instead.

## 3. `mechanism: dlq_redrive`

Both ends are queues. Assert the source's redrive policy still names the DLQ and
still uses the recorded `maxReceiveCount`.

```python
def test_event_analysis_jobs_to_analysis_jobs_dlq(sqs, resolve):
    """Integration point 6a2d3c7315e6.

    aws_sqs_queue.analysis_jobs -> aws_sqs_queue.analysis_jobs_dlq
    type=event_edge mechanism=dlq_redrive max_receive_count=3
    HCL: aws_sqs_queue.analysis_jobs
    """
    import json

    source = resolve("aws_sqs_queue.analysis_jobs")
    dlq = resolve("aws_sqs_queue.analysis_jobs_dlq")

    attributes = sqs.get_queue_attributes(
        QueueUrl=source["url"], AttributeNames=["RedrivePolicy"]
    )["Attributes"]

    assert "RedrivePolicy" in attributes, (
        f"{source['url']} has no redrive policy at all; failed messages are "
        "dropped rather than reaching the DLQ."
    )
    policy = json.loads(attributes["RedrivePolicy"])
    assert policy["deadLetterTargetArn"] == dlq["arn"], (
        f"Redrive targets {policy['deadLetterTargetArn']}, not {dlq['arn']}."
    )
    assert int(policy["maxReceiveCount"]) == 3, (
        f"maxReceiveCount is {policy['maxReceiveCount']}, not the 3 the "
        "integration point records."
    )
```

A drifted `maxReceiveCount` is worth failing on: raising it silently increases
how long a poison message is retried, and lowering it sends healthy messages to
the DLQ early.

## 4. `mechanism: lambda_permission`

The source is a principal (`s3.amazonaws.com`) or a source ARN, not a resource
you can resolve. Assert the function's resource policy still carries the
statement. `get_policy` is read-only, and raises `ResourceNotFoundException`
when the function has no policy at all — that is a finding, not an error.

```python
def test_event_s3_uploads_to_webhook(lambda_, resolve):
    """Integration point <id>.

    aws_s3_bucket.uploads -> aws_lambda_function.webhook
    type=event_edge mechanism=lambda_permission action=lambda:InvokeFunction
      principal=s3.amazonaws.com
    HCL: aws_lambda_permission.allow_s3
    """
    import json

    function = resolve("aws_lambda_function.webhook")

    try:
        raw = lambda_.get_policy(FunctionName=function["function_name"])["Policy"]
    except lambda_.exceptions.ResourceNotFoundException:
        raise AssertionError(
            f"{function['function_name']} has no resource policy, so nothing "
            "may invoke it. The permission is gone."
        ) from None

    statements = json.loads(raw)["Statement"]
    matching = [
        s
        for s in statements
        if s.get("Effect") == "Allow"
        and s.get("Action") == "lambda:InvokeFunction"
        and s.get("Principal", {}).get("Service") == "s3.amazonaws.com"
    ]
    assert matching, (
        f"{function['function_name']} does not allow s3.amazonaws.com to "
        "invoke it. The trigger will not fire."
    )
```

When the point carries a source ARN, tighten the match on the statement's
`Condition.ArnLike["AWS:SourceArn"]` too — without it any bucket in the account
can invoke the function, which is a materially wider grant than the Terraform
declared.

## 5. `mechanism: s3_notification`

The bucket's notification configuration is the declaration; one edge is one
destination in it. `get_bucket_notification_configuration` is read-only and
returns `QueueConfigurations`, `TopicConfigurations`,
`LambdaFunctionConfigurations`, and `EventBridgeConfiguration` — assert against
the list matching the point's target kind.

Assert the filter too. Two edges can share a bucket and a queue and differ only
by `filter_prefix`/`filter_suffix`, and a widened filter fires the target on
objects it was never meant to see.

```python
def test_event_source_bucket_to_resize_queue(s3, resolve):
    """Integration point <id>.

    aws_s3_bucket.source -> aws_sqs_queue.resize
    type=event_edge mechanism=s3_notification events=s3:ObjectCreated:*
      filter_suffix=.jpg
    HCL: aws_s3_bucket_notification.source
    """
    bucket = resolve("aws_s3_bucket.source")
    queue = resolve("aws_sqs_queue.resize")

    config = s3.get_bucket_notification_configuration(Bucket=bucket["bucket"])
    matching = [
        c
        for c in config.get("QueueConfigurations", [])
        if c["QueueArn"] == queue["arn"] and "s3:ObjectCreated:*" in c["Events"]
    ]
    assert matching, (
        f"{bucket['bucket']} does not notify {queue['arn']} on "
        "s3:ObjectCreated:*. Uploads will not reach the queue."
    )

    rules = {
        rule["Name"].lower(): rule["Value"]
        for rule in matching[0].get("Filter", {}).get("Key", {}).get("FilterRules", [])
    }
    assert rules.get("suffix") == ".jpg", (
        f"Suffix filter is {rules.get('suffix')!r}, not '.jpg'. The trigger "
        "fires on a different set of objects than the point records."
    )
```

`filter_prefix` and `filter_suffix` are `""` when unset; assert their absence
the same way, because a filter *added* since the plan silently stops the
target firing on most uploads.

When the destination is a topic or a function, the shape is the same against
`TopicConfigurations` / `LambdaFunctionConfigurations` and their `TopicArn` /
`LambdaFunctionArn` keys.

A target of `eventbridge` is the fifth destination kind: the bucket forwards
every event to the default bus rather than to a named resource. There is
nothing to resolve, and the assertion is that forwarding is on.

```python
def test_event_source_bucket_to_eventbridge(s3, resolve):
    """Integration point <id>.

    aws_s3_bucket.source -> eventbridge
    type=event_edge mechanism=s3_notification events=* external=True
    HCL: aws_s3_bucket_notification.source
    """
    bucket = resolve("aws_s3_bucket.source")

    config = s3.get_bucket_notification_configuration(Bucket=bucket["bucket"])
    assert "EventBridgeConfiguration" in config, (
        f"{bucket['bucket']} no longer forwards events to EventBridge; every "
        "rule matching this bucket has stopped firing."
    )
```

## 6. `mechanism: eventbridge_target`

The source is the rule, the target is what it invokes. Two read-only calls:
`describe_rule` for the rule's state, `list_targets_by_rule` for the wiring.
Both take the bus, and the bus matters — rule names are unique per bus, not per
account, so omitting it silently asks about `default`.

```python
def test_event_rule_to_lambda_function(events, resolve):
    """Integration point <id>.

    aws_cloudwatch_event_rule.event_rule -> aws_lambda_function.lambda_function
    type=event_edge mechanism=eventbridge_target trigger=pattern enabled=True
    HCL: aws_cloudwatch_event_target.target_lambda_function
    """
    rule = resolve("aws_cloudwatch_event_rule.event_rule")
    function = resolve("aws_lambda_function.lambda_function")
    bus = rule.get("event_bus_name") or "default"

    described = events.describe_rule(Name=rule["name"], EventBusName=bus)
    assert described["State"] == "ENABLED", (
        f"Rule {rule['name']} is {described['State']}, not ENABLED. It is "
        "wired but will not fire."
    )

    targets = events.list_targets_by_rule(Rule=rule["name"], EventBusName=bus)
    arns = [target["Arn"] for target in targets["Targets"]]
    assert function["arn"] in arns, (
        f"{function['arn']} is not a target of {rule['name']}. Targets: {arns}"
    )
```

Assert `State` only in the direction the point records. When `enabled` is
false the rule is deliberately off, and a test demanding `ENABLED` reports
drift that is really the declared design; assert `!= "ENABLED"` and say why in
the docstring.

`trigger` says which half of the rule to read if you want a tighter check:
`schedule` means `described["ScheduleExpression"]`, `pattern` means
`described["EventPattern"]`. Comparing a whole event pattern string is brittle
— AWS reformats it — so prefer asserting the key the rule filters on rather
than the document.

A permission is the other half of an EventBridge → Lambda path, and ITest emits
it as its own `lambda_permission` point. Do not fold it into this test: the
target can be present while the function refuses the invoke, and two points
mean two findings.

When `external: true`, the target ARN belongs to another stack. Read it from
the manifest with the `point` fixture rather than pasting it:

```python
def test_event_rule_to_external_queue(events, resolve, point):
    """Integration point <id>.

    aws_cloudwatch_event_rule.nightly -> a queue owned by another stack
    type=event_edge mechanism=eventbridge_target trigger=schedule external=True
    HCL: aws_cloudwatch_event_target.nightly
    """
    rule = resolve("aws_cloudwatch_event_rule.nightly")
    bus = rule.get("event_bus_name") or "default"
    target_arn = point("<id>")["target"]

    targets = events.list_targets_by_rule(Rule=rule["name"], EventBusName=bus)
    arns = [target["Arn"] for target in targets["Targets"]]
    assert target_arn in arns, (
        f"{target_arn} is not a target of {rule['name']} (cross-stack). Targets: {arns}"
    )
```

## 7. Notes across mechanisms

- **`external: true`** means the other end belongs to another stack. Resolve
  only the end that is local, and use the recorded ARN for the other. Say
  "cross-stack" in the docstring: a failure often means the other stack moved.
- **Never send a real message to prove wiring.** Publishing to a queue or
  invoking a function is an active probe, allowed only if the user explicitly
  approved that tier in the interview, and never in prod. Every check above
  reads configuration instead.
- **Existence is not delivery.** These assertions prove the wiring is declared
  and enabled, not that a message arrived. Do not claim more in the docstring.

## 8. What a failure means

A missing mapping, a missing redrive policy, a missing permission statement, a
notification the bucket no longer carries, or a target the rule no longer lists
means the invocation path in the Terraform does not exist in the account. The
event simply will not fire, and this test is how you learn that before an
incident does.

A mapping present but `Disabled` — or an EventBridge rule present but not
`ENABLED` — is the subtler case: the wiring looks right in the console and
delivers nothing. It is a real finding.

A notification whose filter has drifted is subtler still: it fires, just on a
different set of objects. Report the recorded filter and the live one.

`LookupError` from `resolve` means the address is not in the current state —
renamed, removed, or the wrong `terraform_dir`.

Report a failure as a **finding**, naming both ends and the mechanism. Ask the
user whether it is drift or a bad assertion. Never relax a check — an assertion
that only proves a queue exists is not evidence that anything is wired to it.
