"""F8 regression: an IAM grant on arn:aws:s3:::bucket/* resolves to the bucket.

iam_edges indexed bucket ARNs exactly, so an object-space ``bucket/*`` ARN did
not match and the edge resolved external — even with the bucket sitting in the
same stack. It now resolves to the in-stack bucket resource, tagged object-scope,
so the reader sees the bucket by name instead of "external".
"""

from __future__ import annotations

import json

from itest.core.detectors.iam_edges import IamEdgeDetector


def _plan(resources: list[dict]) -> dict:
    return {"values": {"root_module": {"resources": resources}}}


def _role_with_resource(resource_arn: str) -> dict:
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": resource_arn,
                }
            ]
        }
    )
    return {
        "address": "aws_iam_role.app",
        "mode": "managed",
        "type": "aws_iam_role",
        "name": "app",
        "values": {
            "name": "app-role",
            "inline_policy": [{"name": "s3", "policy": policy}],
        },
    }


_BUCKET = {
    "address": "aws_s3_bucket.data",
    "mode": "managed",
    "type": "aws_s3_bucket",
    "name": "data",
    "values": {"arn": "arn:aws:s3:::my-bucket", "bucket": "my-bucket"},
}


def test_object_arn_resolves_to_in_stack_bucket_as_object_scope() -> None:
    points = IamEdgeDetector().detect(
        _plan([_role_with_resource("arn:aws:s3:::my-bucket/*"), _BUCKET])
    )
    assert len(points) == 1
    edge = points[0]
    assert edge.target == "aws_s3_bucket.data"
    assert edge.attributes["external"] is False
    assert edge.attributes["object_scope"] is True
    # It is still a wildcard over the objects.
    assert edge.attributes["wildcard_resource"] is True


def test_prefixed_object_arn_also_resolves() -> None:
    points = IamEdgeDetector().detect(
        _plan([_role_with_resource("arn:aws:s3:::my-bucket/uploads/*"), _BUCKET])
    )
    assert points[0].target == "aws_s3_bucket.data"
    assert points[0].attributes["object_scope"] is True


def test_bucket_level_grant_is_unchanged_no_object_scope() -> None:
    points = IamEdgeDetector().detect(
        _plan([_role_with_resource("arn:aws:s3:::my-bucket"), _BUCKET])
    )
    assert points[0].target == "aws_s3_bucket.data"
    assert points[0].attributes["external"] is False
    assert "object_scope" not in points[0].attributes


def test_object_arn_for_out_of_stack_bucket_stays_external() -> None:
    points = IamEdgeDetector().detect(
        _plan([_role_with_resource("arn:aws:s3:::other-bucket/*")])
    )
    assert points[0].target == "arn:aws:s3:::other-bucket/*"
    assert points[0].attributes["external"] is True
    assert "object_scope" not in points[0].attributes
