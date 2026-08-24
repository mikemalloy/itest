from __future__ import annotations

import json
from pathlib import Path

from itest.core.detectors.base import detect_all
from itest.core.detectors.sg_edges import SecurityGroupEdgeDetector

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "simple-web-app-plan.json"
)


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _edge(points, source, target):
    matches = [p for p in points if p.source == source and p.target == target]
    assert len(matches) == 1, f"expected one edge {source}->{target}, got {matches}"
    return matches[0]


def test_three_expected_edges() -> None:
    points = SecurityGroupEdgeDetector().detect(_load())

    # Exactly the intended chain, nothing more (egress-all rules are ignored).
    assert len(points) == 3

    internet = _edge(points, "0.0.0.0/0", "aws_security_group.alb")
    assert internet.attributes == {
        "protocol": "tcp",
        "ports": "443",
        "direction": "ingress",
    }
    assert internet.hcl_address == "aws_security_group.alb.ingress[0]"

    alb_web = _edge(points, "aws_security_group.alb", "aws_security_group.web")
    assert alb_web.attributes["ports"] == "80"
    assert alb_web.attributes["direction"] == "ingress"
    assert alb_web.hcl_address == "aws_security_group_rule.web_from_alb"

    web_db = _edge(points, "aws_security_group.web", "aws_security_group.db")
    assert web_db.attributes["ports"] == "5432"
    assert web_db.attributes["protocol"] == "tcp"
    assert web_db.hcl_address == "aws_security_group_rule.db_from_web"


def test_ids_stable_across_runs() -> None:
    plan = _load()
    ids1 = sorted(p.id for p in SecurityGroupEdgeDetector().detect(plan))
    ids2 = sorted(p.id for p in SecurityGroupEdgeDetector().detect(_load()))
    assert ids1 == ids2
    assert len(set(ids1)) == 3  # deterministic and collision-free here


def test_detect_all_reports_unanalyzed_types() -> None:
    points, unanalyzed = detect_all(_load())
    assert len(points) == 3

    # SG resources are handled; everything else managed is "not analyzed".
    assert "aws_security_group" not in unanalyzed
    assert "aws_security_group_rule" not in unanalyzed
    assert unanalyzed == {
        "aws_vpc": 1,
        "aws_subnet": 2,
        "aws_lb": 1,
        "aws_instance": 2,
        "aws_db_instance": 1,
    }
