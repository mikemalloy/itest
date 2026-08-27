"""S3 bucket notifications are invocation wiring.

`aws_s3_bucket_notification` is how an upload drives a queue, a topic, or a
function. Terraform models it as one resource holding lists of destinations,
so a single resource can declare several independent edges — and two
notifications to the same queue differing only by filter are two different
integration points.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from itest.core import points
from itest.core.detectors.base import detect_all

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "s3-notification-state.json"

BUCKET = "aws_s3_bucket.source"
QUEUE = "aws_sqs_queue.resize"
NOTIFICATION = "aws_s3_bucket_notification.source"


def _document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _events(document: dict) -> list:
    detected, _ = detect_all(document)
    return [p for p in detected if p.type == "event_edge"]


def _notifications(document: dict) -> list:
    return [
        p
        for p in _events(document)
        if p.attributes.get("mechanism") == "s3_notification"
    ]


def _resources(document: dict) -> list:
    return document["values"]["root_module"]["resources"]


def _notification_values(document: dict) -> dict:
    return next(
        r["values"]
        for r in _resources(document)
        if r["type"] == "aws_s3_bucket_notification"
    )


# --------------------------------------------------------------------------
# The queue destination
# --------------------------------------------------------------------------


def test_one_notification_edge_bucket_to_queue() -> None:
    edges = _notifications(_document())
    assert len(edges) == 1, [(p.source, p.target) for p in edges]

    edge = edges[0]
    assert edge.source == BUCKET
    assert edge.target == QUEUE
    assert edge.hcl_address == NOTIFICATION


def test_notification_attributes() -> None:
    edge = _notifications(_document())[0]
    assert edge.attributes["mechanism"] == "s3_notification"
    assert edge.attributes["events"] == ["s3:ObjectCreated:*"]
    assert edge.attributes["filter_suffix"] == ".jpg"
    assert edge.attributes["filter_prefix"] == ""
    assert edge.attributes["external"] is False


def test_bucket_resolves_by_name_not_arn() -> None:
    """values.bucket is a bare name; the bucket is found by bucket/id."""
    edge = _notifications(_document())[0]
    assert edge.source == BUCKET
    assert not edge.source.startswith("arn:")


def test_unresolvable_destination_is_external() -> None:
    document = _document()
    _resources(document)[:] = [
        r for r in _resources(document) if r["type"] != "aws_sqs_queue"
    ]

    edge = _notifications(document)[0]
    assert edge.target.startswith("arn:aws:sqs:")
    assert edge.attributes["external"] is True


def test_unresolvable_bucket_is_external() -> None:
    document = _document()
    _notification_values(document)["bucket"] = "some-other-bucket"

    edge = _notifications(document)[0]
    assert edge.source == "some-other-bucket"
    assert edge.attributes["external"] is True


# --------------------------------------------------------------------------
# Other destination kinds
# --------------------------------------------------------------------------


def test_lambda_and_topic_destinations_each_emit() -> None:
    document = _document()
    values = _notification_values(document)
    values["lambda_function"] = [
        {
            "events": ["s3:ObjectRemoved:*"],
            "filter_prefix": "",
            "filter_suffix": "",
            "lambda_function_arn": (
                "arn:aws:lambda:us-east-1:111111111111:function:cleanup"
            ),
        }
    ]
    values["topic"] = [
        {
            "events": ["s3:ObjectCreated:Put"],
            "filter_prefix": "raw/",
            "filter_suffix": "",
            "topic_arn": "arn:aws:sns:us-east-1:111111111111:alerts",
        }
    ]

    edges = _notifications(document)
    assert len(edges) == 3
    assert all(p.source == BUCKET for p in edges)
    targets = {p.target for p in edges}
    assert "arn:aws:lambda:us-east-1:111111111111:function:cleanup" in targets
    assert "arn:aws:sns:us-east-1:111111111111:alerts" in targets


def test_eventbridge_true_emits_the_default_bus() -> None:
    document = _document()
    _notification_values(document)["eventbridge"] = True

    edges = _notifications(document)
    bus = [p for p in edges if p.target == "eventbridge"]
    assert len(bus) == 1
    assert bus[0].source == BUCKET
    assert bus[0].attributes["events"] == ["*"]
    assert bus[0].attributes["external"] is True


def test_eventbridge_false_emits_nothing_extra() -> None:
    assert all(p.target != "eventbridge" for p in _notifications(_document()))


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_two_filters_to_one_queue_are_two_points() -> None:
    """Same bucket, same queue, different filter: different integrations."""
    document = _document()
    values = _notification_values(document)
    second = copy.deepcopy(values["queue"][0])
    second["filter_suffix"] = ".png"
    values["queue"].append(second)

    edges = _notifications(document)
    assert len(edges) == 2
    assert len({p.id for p in edges}) == 2


def test_ids_are_stable_across_runs() -> None:
    first = sorted(p.id for p in _notifications(_document()))
    second = sorted(p.id for p in _notifications(_document()))
    assert first == second


def test_notification_type_is_handled() -> None:
    _, unanalyzed = detect_all(_document())
    assert "aws_s3_bucket_notification" not in unanalyzed
    # The bucket itself is not claimed: it is a container, not wiring.
    assert "aws_s3_bucket" in unanalyzed


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------


def test_summary_and_diagram_label() -> None:
    edge = _notifications(_document())[0]
    assert points.summary(edge) == "s3_notification s3:ObjectCreated:* [suffix .jpg]"
    assert points.diagram_label(edge) == "s3_notification"


def test_summary_without_a_filter_omits_the_suffix_clause() -> None:
    document = _document()
    _notification_values(document)["queue"][0]["filter_suffix"] = ""

    edge = _notifications(document)[0]
    assert points.summary(edge) == "s3_notification s3:ObjectCreated:*"
