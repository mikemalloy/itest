# Recipe: `event_edge`

An invocation path. The point asserts "this source triggers that target", as
declared by an event source mapping, a redrive policy, or a Lambda permission.

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
| `attributes.mechanism` | `event_source_mapping`, `dlq_redrive`, or `lambda_permission`. **This decides which assertion to write** — the three are not interchangeable. |
| `attributes.external` | The ARN did not resolve within this state; another stack owns it. |
| `attributes.batch_size`, `enabled` | ESM only. A disabled mapping is wired but not delivering, which is worth asserting explicitly. |
| `attributes.max_receive_count` | DLQ redrive only. How many failures before a message moves to the DLQ. |
| `attributes.action`, `principal` | Lambda permission only, e.g. `lambda:InvokeFunction` and `s3.amazonaws.com`. |
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

## 5. Notes across mechanisms

- **`external: true`** means the other end belongs to another stack. Resolve
  only the end that is local, and use the recorded ARN for the other. Say
  "cross-stack" in the docstring: a failure often means the other stack moved.
- **Never send a real message to prove wiring.** Publishing to a queue or
  invoking a function is an active probe, allowed only if the user explicitly
  approved that tier in the interview, and never in prod. Every check above
  reads configuration instead.
- **Existence is not delivery.** These assertions prove the wiring is declared
  and enabled, not that a message arrived. Do not claim more in the docstring.

## 6. What a failure means

A missing mapping, a missing redrive policy, or a missing permission statement
means the invocation path in the Terraform does not exist in the account. The
event simply will not fire, and this test is how you learn that before an
incident does.

A mapping present but `Disabled` is the subtler case: the wiring looks right in
the console and delivers nothing. It is a real finding.

`LookupError` from `resolve` means the address is not in the current state —
renamed, removed, or the wrong `terraform_dir`.

Report a failure as a **finding**, naming both ends and the mechanism. Ask the
user whether it is drift or a bad assertion. Never relax a check — an assertion
that only proves a queue exists is not evidence that anything is wired to it.
