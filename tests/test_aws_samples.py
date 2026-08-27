"""Pin the compatibility table (docs/compatibility.md) to the sample fixtures.

Each fixture is sanitized `terraform show -json` state from an
aws-samples/serverless-patterns deployment that was applied, implemented via
the bundled skill, verified green against a live account, and destroyed.
If a detector change alters these counts, the table must change with it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from itest.core.detectors.base import detect_all

SAMPLES = Path(__file__).resolve().parent / "fixtures" / "aws-samples"

EXPECTED = {
    "terraform-sqs-lambda.json": {"iam_edge": 3, "event_edge": 3},
    "s3-sqs-lambda-terraform.json": {"iam_edge": 4, "event_edge": 1},
    "lambda-sqs-terraform.json": {"iam_edge": 2},
    "eventbridge-lambda-terraform.json": {"iam_edge": 1, "event_edge": 1},
}

EXPECTED_UNANALYZED = {
    "s3-sqs-lambda-terraform.json": {
        "aws_lambda_function",
        "aws_s3_bucket",
        "aws_s3_bucket_notification",
        "aws_sqs_queue_policy",
    },
    "eventbridge-lambda-terraform.json": {
        "aws_cloudwatch_event_rule",
        "aws_cloudwatch_event_target",
        "aws_lambda_function",
    },
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sample_point_counts(name: str) -> None:
    document = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
    points, unanalyzed = detect_all(document)
    assert dict(Counter(p.type for p in points)) == EXPECTED[name]
    assert len({p.id for p in points}) == len(points), "duplicate point ids"
    if name in EXPECTED_UNANALYZED:
        assert set(unanalyzed) == EXPECTED_UNANALYZED[name]


def test_samples_are_pseudonymized() -> None:
    for path in SAMPLES.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "111111111111" in text or "arn:aws" not in text, path.name
