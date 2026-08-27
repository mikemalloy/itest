"""Regression tests from the first module-nested real-world run.

The fixture is sanitized state from aws-samples/serverless-patterns
terraform-sqs-lambda, built entirely from terraform-aws-modules. It exposed
two bugs alex never could:

- The lambda module names the IAM role identically to the function, and the
  event detector resolved ``function_name`` against *any* resource with that
  name — the role sorts first, so Lambda permissions pointed at the role.
- The module creates two permissions per trigger (current version and
  unqualified alias). They differ only by ``qualifier``; the detector emitted
  two points with the same id.
"""

from __future__ import annotations

import json
from pathlib import Path

from itest.core.detectors.base import detect_all
from itest.core.detectors.event_edges import EventEdgeDetector

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "aws-samples"
    / "terraform-sqs-lambda.json"
)
QUEUE = "module.sqs.aws_sqs_queue.this[0]"
FUNCTION = "module.lambda_function.aws_lambda_function.this[0]"
ROLE = "module.lambda_function.aws_iam_role.lambda[0]"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_lambda_permission_targets_function_not_same_named_role() -> None:
    points = EventEdgeDetector().detect(_load())
    perms = [p for p in points if p.attributes["mechanism"] == "lambda_permission"]
    assert perms, "expected lambda_permission points"
    assert all(p.target == FUNCTION for p in perms), [p.target for p in perms]
    assert all(p.target != ROLE for p in perms)
    assert all(p.source == QUEUE for p in perms)
    assert all(p.attributes["external"] is False for p in perms)


def test_qualified_and_unqualified_permissions_are_distinct_points() -> None:
    points = EventEdgeDetector().detect(_load())
    perms = [p for p in points if p.attributes["mechanism"] == "lambda_permission"]
    assert len(perms) == 2
    assert {p.attributes.get("qualifier") for p in perms} == {"1", ""}
    assert len({p.id for p in perms}) == 2


def test_event_source_mapping_resolves_inside_modules() -> None:
    points = EventEdgeDetector().detect(_load())
    esm = [p for p in points if p.attributes["mechanism"] == "event_source_mapping"]
    assert len(esm) == 1
    assert esm[0].source == QUEUE
    assert esm[0].target == FUNCTION
    assert esm[0].attributes["batch_size"] == 10


def test_all_point_ids_unique_across_detectors() -> None:
    points, unanalyzed = detect_all(_load())
    ids = [p.id for p in points]
    assert len(ids) == len(set(ids)), "duplicate point ids"
    assert len(points) == 6
    # Packaging plumbing is expected to be unanalyzed; nothing else should be.
    assert set(unanalyzed) == {
        "aws_cloudwatch_log_group",
        "aws_lambda_function",
        "local_file",
        "null_resource",
        "random_pet",
        "terraform_data",
    }
