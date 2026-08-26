"""Event/invocation edge detector tests.

Counts against the alex-s6 fixture were derived by hand: one event source
mapping (analysis_jobs -> planner) and one DLQ redrive (analysis_jobs ->
analysis_jobs_dlq). Disagreement is a loud failure, not a reason to edit the
expectation.
"""

from __future__ import annotations

import json
from pathlib import Path

from itest.core.detectors.base import detect_all
from itest.core.detectors.event_edges import EventEdgeDetector
from itest.core.mermaid import generate_mermaid

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(rel: str) -> dict:
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def _one(points, source, target):
    matches = [p for p in points if p.source == source and p.target == target]
    assert len(matches) == 1, f"expected one edge {source}->{target}, got {matches}"
    return matches[0]


def test_alex_s6_exactly_two_event_edges() -> None:
    points = EventEdgeDetector().detect(_load("alex/alex-s6.json"))
    assert len(points) == 2, [(p.source, p.target) for p in points]
    assert all(p.type == "event_edge" for p in points)

    esm = _one(points, "aws_sqs_queue.analysis_jobs", "aws_lambda_function.planner")
    assert esm.attributes["mechanism"] == "event_source_mapping"
    assert esm.attributes["batch_size"] == 1
    assert esm.attributes["enabled"] is True
    assert esm.attributes["external"] is False
    assert esm.hcl_address == "aws_lambda_event_source_mapping.planner_sqs"

    dlq = _one(points, "aws_sqs_queue.analysis_jobs", "aws_sqs_queue.analysis_jobs_dlq")
    assert dlq.attributes["mechanism"] == "dlq_redrive"
    assert dlq.attributes["max_receive_count"] == 3
    assert dlq.attributes["external"] is False
    assert dlq.hcl_address == "aws_sqs_queue.analysis_jobs"


def test_synthetic_lambda_permission_edges() -> None:
    points = EventEdgeDetector().detect(_load("lambda-permission-state.json"))
    assert len(points) == 2, [(p.source, p.target) for p in points]

    s3 = _one(points, "aws_s3_bucket.uploads", "aws_lambda_function.webhook")
    assert s3.attributes["mechanism"] == "lambda_permission"
    assert s3.attributes["action"] == "lambda:InvokeFunction"
    assert s3.attributes["principal"] == "s3.amazonaws.com"
    assert s3.attributes["external"] is False
    assert s3.hcl_address == "aws_lambda_permission.allow_s3"

    # source_arn not present in the document: stays an ARN, flagged external;
    # function_name given as a full ARN still resolves.
    apigw = _one(
        points,
        "arn:aws:execute-api:us-west-1:111111111111:abc123def4/*/POST/hook",
        "aws_lambda_function.webhook",
    )
    assert apigw.attributes["principal"] == "apigateway.amazonaws.com"
    assert apigw.attributes["external"] is True


def test_lambda_permission_without_source_arn_uses_principal() -> None:
    plan = {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_lambda_permission.p",
                        "mode": "managed",
                        "type": "aws_lambda_permission",
                        "name": "p",
                        "values": {
                            "action": "lambda:InvokeFunction",
                            "function_name": "fn",
                            "principal": "events.amazonaws.com",
                        },
                    }
                ]
            }
        }
    }
    points = EventEdgeDetector().detect(plan)
    assert len(points) == 1
    assert points[0].source == "events.amazonaws.com"
    assert points[0].target == "fn"
    assert points[0].attributes["external"] is True


def test_ids_stable_across_runs() -> None:
    a = {p.id for p in EventEdgeDetector().detect(_load("alex/alex-s6.json"))}
    b = {p.id for p in EventEdgeDetector().detect(_load("alex/alex-s6.json"))}
    assert a == b


def test_event_types_no_longer_unanalyzed() -> None:
    _, unanalyzed = detect_all(_load("alex/alex-s6.json"))
    assert "aws_lambda_event_source_mapping" not in unanalyzed
    assert "aws_lambda_permission" not in unanalyzed
    # aws_sqs_queue is only *claimed* for its redrive wiring; the detector
    # still reports it as handled so plan output does not double-count it.
    assert "aws_sqs_queue" not in unanalyzed


def test_mermaid_labels_distinguish_point_types() -> None:
    points, _ = detect_all(_load("alex/alex-s6.json"))
    diagram = generate_mermaid(points)
    assert "|event_source_mapping|" in diagram
    assert "|dlq_redrive|" in diagram
    assert "|managed|" in diagram
    assert "|lambda:InvokeFunction|" in diagram
