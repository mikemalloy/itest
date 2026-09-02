"""F10 regression: two lambda_permission grants differing only by action.

The lambda_permission point id omitted action, so two grants from one principal
(InvokeFunction vs InvokeFunctionUrl, no source_arn) collapsed to one point. The
action is appended to the id ONLY when such a collision is actually present, so
every non-colliding permission id is unchanged (append-only, per the id-stability
invariant).
"""

from __future__ import annotations

from itest.core.detectors.event_edges import EventEdgeDetector, _point_id

_FUNC = {
    "address": "aws_lambda_function.api",
    "mode": "managed",
    "type": "aws_lambda_function",
    "name": "api",
    "values": {
        "function_name": "my-func",
        "arn": "arn:aws:lambda:us-east-1:111111111111:function:my-func",
    },
}


def _perm(name: str, action: str, principal: str = "apigateway.amazonaws.com") -> dict:
    return {
        "address": f"aws_lambda_permission.{name}",
        "mode": "managed",
        "type": "aws_lambda_permission",
        "name": name,
        "values": {
            "principal": principal,
            "action": action,
            "function_name": "my-func",
        },
    }


def _plan(resources: list[dict]) -> dict:
    return {"values": {"root_module": {"resources": resources}}}


def _perms(points) -> list:
    return [p for p in points if p.attributes.get("mechanism") == "lambda_permission"]


def test_two_actions_one_principal_are_distinct_points() -> None:
    points = EventEdgeDetector().detect(
        _plan(
            [
                _perm("invoke", "lambda:InvokeFunction"),
                _perm("invokeurl", "lambda:InvokeFunctionUrl"),
                _FUNC,
            ]
        )
    )
    perms = _perms(points)
    assert len(perms) == 2
    assert len({p.id for p in perms}) == 2


def test_lone_permission_id_is_unchanged_no_action_discriminator() -> None:
    """A permission with no colliding sibling keeps its original id — the action
    is not folded in, so ids that predate this fix are stable."""
    points = EventEdgeDetector().detect(
        _plan([_perm("invoke", "lambda:InvokeFunction"), _FUNC])
    )
    perms = _perms(points)
    assert len(perms) == 1
    # The original id shape: source, target, mechanism, qualifier — no action.
    expected = _point_id(
        "apigateway.amazonaws.com", "aws_lambda_function.api", "lambda_permission", ""
    )
    assert perms[0].id == expected


def test_identical_duplicate_grants_still_dedupe() -> None:
    """Two grants identical in every way (same action) remain one point."""
    points = EventEdgeDetector().detect(
        _plan(
            [
                _perm("a", "lambda:InvokeFunction"),
                _perm("b", "lambda:InvokeFunction"),
                _FUNC,
            ]
        )
    )
    assert len(_perms(points)) == 1
