"""F11 regression: two EventBridge targets on one rule differing only by input.

The eventbridge_target discriminator was ``bus=`` only, so two targets on one
rule to one destination that differ only by their ``input`` collapsed to one
point. A content-based input discriminator is appended ONLY when such a collision
is present, so every non-colliding target id is unchanged (append-only).
"""

from __future__ import annotations

from itest.core.detectors.event_edges import EventEdgeDetector, _point_id

_RULE = {
    "address": "aws_cloudwatch_event_rule.r",
    "mode": "managed",
    "type": "aws_cloudwatch_event_rule",
    "name": "r",
    "values": {"name": "r", "schedule_expression": "rate(5 minutes)"},
}
_FUNC = {
    "address": "aws_lambda_function.fn",
    "mode": "managed",
    "type": "aws_lambda_function",
    "name": "fn",
    "values": {
        "function_name": "fn",
        "arn": "arn:aws:lambda:us-east-1:111111111111:function:fn",
    },
}


def _target(name: str, **extra) -> dict:
    return {
        "address": f"aws_cloudwatch_event_target.{name}",
        "mode": "managed",
        "type": "aws_cloudwatch_event_target",
        "name": name,
        "values": {
            "rule": "r",
            "arn": "arn:aws:lambda:us-east-1:111111111111:function:fn",
            **extra,
        },
    }


def _plan(resources: list[dict]) -> dict:
    return {"values": {"root_module": {"resources": resources}}}


def _targets(points) -> list:
    return [p for p in points if p.attributes.get("mechanism") == "eventbridge_target"]


def test_two_inputs_one_rule_one_target_are_distinct() -> None:
    points = EventEdgeDetector().detect(
        _plan(
            [
                _RULE,
                _FUNC,
                _target("t1", input='{"a": 1}'),
                _target("t2", input='{"b": 2}'),
            ]
        )
    )
    targets = _targets(points)
    assert len(targets) == 2
    assert len({p.id for p in targets}) == 2


def test_lone_target_id_is_unchanged_no_input_discriminator() -> None:
    points = EventEdgeDetector().detect(
        _plan([_RULE, _FUNC, _target("t1", input='{"a": 1}')])
    )
    targets = _targets(points)
    assert len(targets) == 1
    expected = _point_id(
        "aws_cloudwatch_event_rule.r",
        "aws_lambda_function.fn",
        "eventbridge_target",
        None,
        "bus=default",
    )
    assert targets[0].id == expected


def test_identical_targets_still_dedupe() -> None:
    points = EventEdgeDetector().detect(
        _plan(
            [
                _RULE,
                _FUNC,
                _target("t1", input='{"same": 1}'),
                _target("t2", input='{"same": 1}'),
            ]
        )
    )
    assert len(_targets(points)) == 1
