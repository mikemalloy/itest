"""API Gateway route -> integration edges, against the sanitized alex states.

A route is where the internet reaches the stack, so the edge worth pinning is
route -> what it invokes: alex-s3 is a REST (v1) API in front of a Lambda,
alex-s7 an HTTP (v2) API in front of another. Both are production states, so
the counts below were read out of the fixtures rather than invented.

`auth: NONE` is a finding-class attribute here, the way `wildcard_resource` is
for an IAM edge: an unauthenticated route is not a bug by itself, but it is
something a reviewer must see in plan output.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from itest.core import points as point_labels
from itest.core.detectors.base import detect_all
from itest.core.detectors.route_edges import RouteEdgeDetector

ALEX = Path(__file__).resolve().parent / "fixtures" / "alex"


def _load(name: str) -> dict:
    return json.loads((ALEX / name).read_text(encoding="utf-8"))


def _routes(document: dict) -> list:
    return RouteEdgeDetector().detect(document)


def _by_method(document: dict) -> dict:
    return {p.attributes["method"]: p for p in _routes(document)}


def _resources(document: dict) -> list:
    return document["values"]["root_module"]["resources"]


def _find(document: dict, rtype: str) -> dict:
    return next(r for r in _resources(document) if r["type"] == rtype)


# --------------------------------------------------------------------------
# alex-s3: REST API (v1)
# --------------------------------------------------------------------------


def test_alex_s3_yields_one_route_edge() -> None:
    edges = _routes(_load("alex-s3.json"))
    assert len(edges) == 1, [(p.source, p.target) for p in edges]

    edge = edges[0]
    assert edge.type == "route_edge"
    assert edge.source == "aws_api_gateway_rest_api.api"
    assert edge.target == "aws_lambda_function.ingest"
    assert edge.hcl_address == "aws_api_gateway_method.ingest_post"


def test_alex_s3_route_attributes() -> None:
    edge = _routes(_load("alex-s3.json"))[0]
    assert edge.attributes["method"] == "POST"
    assert edge.attributes["path"] == "/ingest"
    assert edge.attributes["integration_type"] == "AWS_PROXY"
    assert edge.attributes["auth"] == "NONE"
    assert edge.attributes["api_key_required"] is True
    assert edge.attributes["stages"] == ["prod"]
    assert edge.attributes["external"] is False


# --------------------------------------------------------------------------
# alex-s7: HTTP API (v2)
# --------------------------------------------------------------------------


def test_alex_s7_yields_two_route_edges() -> None:
    edges = _routes(_load("alex-s7.json"))
    assert len(edges) == 2, [(p.attributes.get("method"), p.target) for p in edges]
    assert {p.source for p in edges} == {"aws_apigatewayv2_api.main"}
    assert {p.target for p in edges} == {"aws_lambda_function.api"}


def test_alex_s7_route_attributes() -> None:
    edges = _by_method(_load("alex-s7.json"))
    assert set(edges) == {"ANY", "OPTIONS"}
    for edge in edges.values():
        assert edge.attributes["path"] == "/api/{proxy+}"
        assert edge.attributes["integration_type"] == "AWS_PROXY"
        assert edge.attributes["auth"] == "NONE"
        assert edge.attributes["api_key_required"] is False
        assert edge.attributes["stages"] == ["$default"]
        assert edge.attributes["external"] is False


def test_alex_s7_hcl_address_is_the_route() -> None:
    edges = _by_method(_load("alex-s7.json"))
    assert edges["ANY"].hcl_address == "aws_apigatewayv2_route.api_any"
    assert edges["OPTIONS"].hcl_address == "aws_apigatewayv2_route.api_options"


# --------------------------------------------------------------------------
# Synthetic variations
# --------------------------------------------------------------------------


def test_v1_mock_integration_is_external() -> None:
    """MOCK answers from the gateway itself: nothing in this state to resolve."""
    document = _load("alex-s3.json")
    integration = _find(document, "aws_api_gateway_integration")["values"]
    integration["type"] = "MOCK"
    integration["uri"] = ""

    edge = _routes(document)[0]
    assert edge.attributes["integration_type"] == "MOCK"
    assert edge.attributes["external"] is True
    assert edge.target == "MOCK"


def test_v1_http_integration_targets_the_uri() -> None:
    document = _load("alex-s3.json")
    integration = _find(document, "aws_api_gateway_integration")["values"]
    integration["type"] = "HTTP_PROXY"
    integration["uri"] = "https://upstream.example.internal/ingest"

    edge = _routes(document)[0]
    assert edge.target == "https://upstream.example.internal/ingest"
    assert edge.attributes["external"] is True


def test_v1_unresolvable_lambda_uri_is_external() -> None:
    """The function lives in another stack: keep the ARN, flag it."""
    document = _load("alex-s3.json")
    _resources(document)[:] = [
        r for r in _resources(document) if r["type"] != "aws_lambda_function"
    ]

    edge = _routes(document)[0]
    assert edge.target == "arn:aws:lambda:us-west-1:111111111111:function:alex-ingest"
    assert edge.attributes["external"] is True


def test_v1_method_on_the_root_resource_is_slash() -> None:
    """The root resource is not a resource of its own; its path is "/"."""
    document = _load("alex-s3.json")
    api = _find(document, "aws_api_gateway_rest_api")["values"]
    root_id = api["root_resource_id"]
    _find(document, "aws_api_gateway_method")["values"]["resource_id"] = root_id
    _find(document, "aws_api_gateway_integration")["values"]["resource_id"] = root_id

    edge = _routes(document)[0]
    assert edge.attributes["path"] == "/"


def test_v1_method_without_an_integration_emits_nothing() -> None:
    """A method with nothing behind it invokes nothing; it is not an edge."""
    document = _load("alex-s3.json")
    _resources(document)[:] = [
        r for r in _resources(document) if r["type"] != "aws_api_gateway_integration"
    ]
    assert _routes(document) == []


def test_v2_default_route_is_any_on_a_wildcard_path() -> None:
    document = _load("alex-s7.json")
    _resources(document)[:] = [
        r
        for r in _resources(document)
        if r["address"] != "aws_apigatewayv2_route.api_options"
    ]
    route = _find(document, "aws_apigatewayv2_route")["values"]
    route["route_key"] = "$default"

    edge = _routes(document)[0]
    assert edge.attributes["method"] == "ANY"
    assert edge.attributes["path"] == "/*"


def test_v2_route_without_a_matching_integration_emits_nothing() -> None:
    document = _load("alex-s7.json")
    _find(document, "aws_apigatewayv2_integration")["values"]["id"] = "somethingelse"
    assert _routes(document) == []


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_ids_are_stable_across_runs() -> None:
    for name in ("alex-s3.json", "alex-s7.json"):
        first = sorted(p.id for p in _routes(_load(name)))
        second = sorted(p.id for p in _routes(_load(name)))
        assert first == second, name


def test_two_methods_on_one_path_are_two_points() -> None:
    edges = _routes(_load("alex-s7.json"))
    assert len({p.id for p in edges}) == 2


def test_id_ignores_attributes_outside_the_identity_tuple() -> None:
    """Identity is (type, api, method, path, target) — not every attribute."""
    document = _load("alex-s3.json")
    before = _routes(document)[0].id

    _find(document, "aws_api_gateway_method")["values"]["api_key_required"] = False
    assert _routes(document)[0].id == before


def test_id_changes_with_the_method() -> None:
    document = _load("alex-s3.json")
    before = _routes(document)[0].id

    changed = copy.deepcopy(document)
    _find(changed, "aws_api_gateway_method")["values"]["http_method"] = "GET"
    _find(changed, "aws_api_gateway_integration")["values"]["http_method"] = "GET"
    assert _routes(changed)[0].id != before


# --------------------------------------------------------------------------
# Registration and the unanalyzed map
# --------------------------------------------------------------------------


def test_alex_s3_route_types_leave_the_unanalyzed_map() -> None:
    detected, unanalyzed = detect_all(_load("alex-s3.json"))
    assert len(detected) == 7  # 6 before route_edge, plus the one route
    for claimed in (
        "aws_api_gateway_rest_api",
        "aws_api_gateway_resource",
        "aws_api_gateway_method",
        "aws_api_gateway_integration",
        "aws_api_gateway_deployment",
        "aws_api_gateway_stage",
    ):
        assert claimed not in unanalyzed, claimed
    # Not claimed: these are throttling and credentials, not wiring.
    for unclaimed in (
        "aws_api_gateway_api_key",
        "aws_api_gateway_usage_plan",
        "aws_api_gateway_usage_plan_key",
    ):
        assert unclaimed in unanalyzed, unclaimed


def test_alex_s7_route_types_leave_the_unanalyzed_map() -> None:
    detected, unanalyzed = detect_all(_load("alex-s7.json"))
    assert len(detected) == 12  # 10 before route_edge, plus two routes
    for claimed in (
        "aws_apigatewayv2_api",
        "aws_apigatewayv2_route",
        "aws_apigatewayv2_integration",
        "aws_apigatewayv2_stage",
    ):
        assert claimed not in unanalyzed, claimed
    # CloudFront is a separate mechanism and still has no detector.
    assert "aws_cloudfront_distribution" in unanalyzed


def test_alex_s6_is_untouched() -> None:
    """No API Gateway in s6: the new detector must not move its counts."""
    detected, unanalyzed = detect_all(_load("alex-s6.json"))
    assert len(detected) == 14
    assert not [p for p in detected if p.type == "route_edge"]
    assert unanalyzed == {
        "aws_cloudwatch_log_group": 5,
        "aws_lambda_function": 5,
        "aws_s3_bucket": 1,
        "aws_s3_object": 5,
    }


# --------------------------------------------------------------------------
# Presentation: an open route must be visible in plan output
# --------------------------------------------------------------------------


def test_open_route_is_flagged_in_the_summary() -> None:
    edge = _routes(_load("alex-s3.json"))[0]
    assert point_labels.summary(edge) == "POST /ingest -> AWS_PROXY [open]"


def test_authorized_route_carries_no_open_flag() -> None:
    document = _load("alex-s3.json")
    _find(document, "aws_api_gateway_method")["values"]["authorization"] = "AWS_IAM"

    edge = _routes(document)[0]
    assert point_labels.summary(edge) == "POST /ingest -> AWS_PROXY"
