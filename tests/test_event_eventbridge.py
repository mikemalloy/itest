"""EventBridge rule -> target edges.

A rule on its own invokes nothing; a target is what makes it wiring. Each
target therefore emits one edge, sourced at the rule it belongs to, and a
rule with no targets emits nothing at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from itest.core import points
from itest.core.detectors.base import detect_all

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "aws-samples"
    / "eventbridge-lambda-terraform.json"
)

RULE = "aws_cloudwatch_event_rule.event_rule"
FUNCTION = "aws_lambda_function.lambda_function"


def _document() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def _resources(document: dict) -> list:
    return document["values"]["root_module"]["resources"]


def _targets(document: dict) -> list:
    detected, _ = detect_all(document)
    return [
        p for p in detected if p.attributes.get("mechanism") == "eventbridge_target"
    ]


def _find(document: dict, rtype: str) -> dict:
    return next(r for r in _resources(document) if r["type"] == rtype)


# --------------------------------------------------------------------------
# The real sample
# --------------------------------------------------------------------------


def test_sample_yields_one_eventbridge_edge() -> None:
    edges = _targets(_document())
    assert len(edges) == 1, [(p.source, p.target) for p in edges]

    edge = edges[0]
    assert edge.source == RULE
    assert edge.target == FUNCTION
    assert edge.hcl_address == "aws_cloudwatch_event_target.target_lambda_function"


def test_sample_edge_attributes() -> None:
    edge = _targets(_document())[0]
    assert edge.attributes["trigger"] == "pattern"
    assert edge.attributes["enabled"] is True
    assert edge.attributes["external"] is False
    assert edge.attributes["event_bus_name"] == "default"


def test_lambda_permission_edge_is_still_present() -> None:
    detected, _ = detect_all(_document())
    mechanisms = {
        p.attributes.get("mechanism") for p in detected if p.type == "event_edge"
    }
    assert "lambda_permission" in mechanisms


def test_sample_total_points() -> None:
    detected, unanalyzed = detect_all(_document())
    assert len(detected) == 3
    # Both EventBridge types are claimed now; the function stays unclaimed.
    assert "aws_cloudwatch_event_rule" not in unanalyzed
    assert "aws_cloudwatch_event_target" not in unanalyzed
    assert "aws_lambda_function" in unanalyzed


# --------------------------------------------------------------------------
# Synthetic variations
# --------------------------------------------------------------------------


def _schedule_document() -> dict:
    """A schedule rule pointing at a queue that is not in this state."""
    return {
        "format_version": "1.0",
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_cloudwatch_event_rule.nightly",
                        "mode": "managed",
                        "type": "aws_cloudwatch_event_rule",
                        "name": "nightly",
                        "values": {
                            "name": "nightly",
                            "id": "nightly",
                            "arn": "arn:aws:events:us-east-1:111111111111:rule/nightly",
                            "schedule_expression": "rate(1 day)",
                            "event_bus_name": "default",
                            "state": "ENABLED",
                        },
                        "sensitive_values": {},
                    },
                    {
                        "address": "aws_cloudwatch_event_target.nightly",
                        "mode": "managed",
                        "type": "aws_cloudwatch_event_target",
                        "name": "nightly",
                        "values": {
                            "rule": "nightly",
                            "event_bus_name": "default",
                            "target_id": "terraform-2026",
                            "arn": "arn:aws:sqs:us-east-1:111111111111:elsewhere",
                        },
                        "sensitive_values": {},
                    },
                ]
            }
        },
    }


def test_schedule_rule_to_absent_queue_is_external() -> None:
    edge = _targets(_schedule_document())[0]
    assert edge.source == "aws_cloudwatch_event_rule.nightly"
    assert edge.target == "arn:aws:sqs:us-east-1:111111111111:elsewhere"
    assert edge.attributes["trigger"] == "schedule"
    assert edge.attributes["external"] is True
    assert edge.attributes["enabled"] is True


def test_disabled_rule_reports_enabled_false() -> None:
    document = _schedule_document()
    _find(document, "aws_cloudwatch_event_rule")["values"]["state"] = "DISABLED"

    edge = _targets(document)[0]
    assert edge.attributes["enabled"] is False


def test_rule_without_targets_emits_nothing() -> None:
    document = _schedule_document()
    _resources(document)[:] = [
        r for r in _resources(document) if r["type"] != "aws_cloudwatch_event_target"
    ]
    assert _targets(document) == []


def test_target_on_a_different_bus_does_not_bind() -> None:
    """Rule names are scoped to a bus; same name on another bus is not it."""
    document = _schedule_document()
    _find(document, "aws_cloudwatch_event_target")["values"]["event_bus_name"] = (
        "other-bus"
    )
    assert _targets(document) == []


# --------------------------------------------------------------------------
# Identity and presentation
# --------------------------------------------------------------------------


def test_id_ignores_the_generated_target_id() -> None:
    """target_id is Terraform-generated noise and must not enter the id."""
    document = _schedule_document()
    before = _targets(document)[0].id

    _find(document, "aws_cloudwatch_event_target")["values"]["target_id"] = (
        "terraform-9999999999999999"
    )
    assert _targets(document)[0].id == before


def test_ids_are_stable_across_runs() -> None:
    first = sorted(p.id for p in _targets(_document()))
    second = sorted(p.id for p in _targets(_document()))
    assert first == second


def test_summary_and_diagram_label() -> None:
    edge = _targets(_document())[0]
    assert points.summary(edge) == "eventbridge_target pattern"
    assert points.diagram_label(edge) == "eventbridge"


def test_disabled_shows_in_the_summary() -> None:
    document = _schedule_document()
    _find(document, "aws_cloudwatch_event_rule")["values"]["state"] = "DISABLED"

    edge = _targets(document)[0]
    assert points.summary(edge) == "eventbridge_target schedule disabled"
