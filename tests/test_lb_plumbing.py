"""`lb_edge` must reach every place ITest prints or generates.

The failure this guards against is silent: the detector ships, and the plan
changeset, the diagram, the stub name, or the verify point line renders an
empty tag for it. Adding a type means adding it to `points.py`; stub routing,
sync and verify are supposed to follow with no change at all, and these tests
are how "supposed to" is checked.

The chain has two hops under one type, so both renderings are pinned: a
listener edge reads as the routing decision, a target-group edge as what
serves the group, and a group nothing feeds leads with `[empty]`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core import points as point_labels
from itest.core import stubgen
from itest.core.manifest import IntegrationPoint, load_manifest
from itest.core.mermaid import generate_mermaid

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "aws-samples" / "ecs-fargate-alb.json"

LB_FILE = "itest_tests/test_lb_edges.py"

LB = "module.alb.aws_lb.this[0]"
TG_MAIN = 'module.alb.aws_lb_target_group.this["ex-ecs"]'
TG_ALT = 'module.alb.aws_lb_target_group.this["ex-ecs-alternate"]'
SERVICE = "module.ecs_service.aws_ecs_service.this[0]"


def _listener_point(**overrides) -> IntegrationPoint:
    now = datetime.now(UTC)
    attributes = {
        "hop": "listener",
        "port": 443,
        "protocol": "HTTPS",
        "rule": "default",
        "action": "forward",
        "external": False,
    }
    attributes.update(overrides.pop("attributes", {}))
    data = {
        "id": "0123456789ab",
        "type": "lb_edge",
        "source": "aws_lb.main",
        "target": "aws_lb_target_group.web",
        "attributes": attributes,
        "hcl_address": "aws_lb_listener.https",
        "first_seen": now,
        "last_seen": now,
    }
    data.update(overrides)
    return IntegrationPoint(**data)


def _group_point(**overrides) -> IntegrationPoint:
    now = datetime.now(UTC)
    attributes = {
        "hop": "target_group",
        "via": "ecs_service",
        "container_name": "web",
        "container_port": 3000,
        "health_check_path": "/healthz",
        "target_type": "ip",
        "deployment_role": "production",
        "cluster": "aws_ecs_cluster.main",
        "task_definition": "aws_ecs_task_definition.web",
        "external": False,
    }
    attributes.update(overrides.pop("attributes", {}))
    data = {
        "id": "ba9876543210",
        "type": "lb_edge",
        "source": "aws_lb_target_group.web",
        "target": "aws_ecs_service.web",
        "attributes": attributes,
        "hcl_address": "aws_ecs_service.web",
        "first_seen": now,
        "last_seen": now,
    }
    data.update(overrides)
    return IntegrationPoint(**data)


# --------------------------------------------------------------------------
# Manifest schema
# --------------------------------------------------------------------------


def test_manifest_accepts_the_new_type() -> None:
    assert _listener_point().type == "lb_edge"


def test_manifest_still_rejects_an_unknown_type() -> None:
    """The literal is a guard, not a formality."""
    with pytest.raises(ValueError):
        _listener_point(type="teapot_edge")


# --------------------------------------------------------------------------
# Labels: listener hop
# --------------------------------------------------------------------------


def test_listener_summary_is_protocol_port_target_and_rule() -> None:
    assert point_labels.summary(_listener_point()) == "HTTPS:443 -> web [default]"


def test_listener_summary_shows_the_rule_and_its_conditions() -> None:
    point = _listener_point(
        attributes={"rule": "priority 10 host=api.example.com path=/v1/*"}
    )
    assert point_labels.summary(point) == (
        "HTTPS:443 -> web [priority 10 host=api.example.com path=/v1/*]"
    )


def test_listener_summary_shows_a_weight_only_when_weighted() -> None:
    plain = point_labels.summary(_listener_point())
    weighted = point_labels.summary(_listener_point(attributes={"weight": 30}))
    assert "[weight" not in plain
    assert weighted.endswith("[weight 30]")


def test_listener_summary_flags_a_redirect_as_external() -> None:
    point = _listener_point(
        target="redirect", attributes={"action": "redirect", "external": True}
    )
    assert point_labels.summary(point) == "HTTPS:443 -> redirect [default] [external]"


def test_listener_diagram_label_is_terse() -> None:
    assert point_labels.diagram_label(_listener_point()) == "HTTPS:443"
    ruled = _listener_point(attributes={"rule": "priority 2 path=/*"})
    assert point_labels.diagram_label(ruled) == "HTTPS:443 p2"


# --------------------------------------------------------------------------
# Labels: target-group hop
# --------------------------------------------------------------------------


def test_group_summary_names_the_service_port_and_health_path() -> None:
    assert point_labels.summary(_group_point()) == "-> web :3000 [health /healthz]"


def test_group_summary_marks_the_blue_green_alternate() -> None:
    point = _group_point(attributes={"deployment_role": "alternate"})
    assert point_labels.summary(point).endswith("[alternate]")


def test_empty_group_summary_leads_with_the_empty_flag() -> None:
    """An ALB routing into an empty group is the finding; it must read first."""
    point = _group_point(
        target="(empty)",
        attributes={
            "via": "none",
            "external": True,
            "container_name": None,
            "container_port": None,
            "deployment_role": None,
        },
    )
    tag = point_labels.summary(point)
    assert tag.startswith("[empty]")
    assert tag == "[empty] nothing feeds web"
    assert point_labels.diagram_label(point) == "empty"


def test_attachment_summary_uses_the_attachment_port() -> None:
    point = _group_point(
        target="aws_instance.web",
        attributes={
            "via": "attachment",
            "port": 8080,
            "container_name": None,
            "container_port": None,
            "deployment_role": None,
        },
    )
    assert point_labels.summary(point) == "-> web :8080 [health /healthz]"
    assert point_labels.diagram_label(point) == ":8080"


def test_group_diagram_label_is_the_container_port() -> None:
    assert point_labels.diagram_label(_group_point()) == ":3000"


def test_diagram_carries_both_hops() -> None:
    diagram = generate_mermaid([_listener_point(), _group_point()])
    assert "|HTTPS:443|" in diagram
    assert "|:3000|" in diagram
    assert '["aws_lb.main"]' in diagram


# --------------------------------------------------------------------------
# Function names and stub routing
# --------------------------------------------------------------------------


def test_function_names_are_test_lb() -> None:
    assert point_labels.function_name(_listener_point()).startswith("test_lb_")
    assert point_labels.function_name(_group_point()).startswith("test_lb_")


def test_listener_function_name_carries_protocol_port_and_rule() -> None:
    assert (
        point_labels.function_name(_listener_point())
        == "test_lb_main_HTTPS_443_to_web_default"
    )


def test_function_names_differ_per_rule_on_one_listener() -> None:
    """One listener forwards to one group from several rules."""
    first = point_labels.function_name(_listener_point())
    second = point_labels.function_name(
        _listener_point(attributes={"rule": "priority 2 path=/*"})
    )
    assert first != second


def test_group_function_name_carries_the_feed_and_the_port() -> None:
    assert (
        point_labels.function_name(_group_point())
        == "test_lb_web_to_web_ecs_service_3000"
    )


def test_empty_group_function_name_needs_no_port() -> None:
    point = _group_point(
        target="(empty)",
        attributes={"via": "none", "container_port": None, "external": True},
    )
    assert point_labels.function_name(point) == "test_lb_web_to_empty_none"


def test_stubs_route_to_their_own_file() -> None:
    for point in (_listener_point(), _group_point()):
        assert stubgen.stub_file_for(point) == LB_FILE


# --------------------------------------------------------------------------
# sync and verify need no change of their own — prove it on the fixture
# --------------------------------------------------------------------------


@pytest.fixture
def synced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_sync_writes_six_lb_stubs(synced) -> None:
    text = (synced / LB_FILE).read_text(encoding="utf-8")
    assert text.count("\ndef test_") == 6
    assert text.count("\ndef test_lb_") == 6

    manifest = load_manifest(synced / ".itest" / "manifest.yaml")
    lb_points = [p for p in manifest.points if p.type == "lb_edge"]
    assert len(lb_points) == 6
    for point in lb_points:
        entry = next(t for t in manifest.tests if t.point_id == point.id)
        assert entry.path == LB_FILE
        assert entry.test_name.startswith("test_lb_")


def test_generated_lb_file_is_importable(synced) -> None:
    source = (synced / LB_FILE).read_text(encoding="utf-8")
    compile(source, LB_FILE, "exec")


def test_stub_function_names_are_unique(synced) -> None:
    manifest = load_manifest(synced / ".itest" / "manifest.yaml")
    names = [t.test_name for t in manifest.tests if t.path == LB_FILE]
    assert len(set(names)) == len(names) == 6


def test_stub_docstring_carries_both_hops(synced) -> None:
    """The generic attribute line already covers a new type's attributes."""
    text = (synced / LB_FILE).read_text(encoding="utf-8")
    assert "type=lb_edge" in text
    assert "hop=listener" in text
    assert "hop=target_group" in text
    assert "rule=priority 1 path=/*" in text
    assert "container_port=3000" in text
    assert f"{TG_MAIN} -> {SERVICE}" in text


def test_verify_reports_lb_points_with_their_tags(synced) -> None:
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output

    # The regression this guards: a type points.py does not know renders an
    # empty tag slot.
    assert "(:)" not in result.output
    assert "12 integration points" in result.output
    assert "12 stubs" in result.output

    assert f"(HTTP:80 -> {TG_MAIN} [priority 1 path=/*] [weight 100])" in result.output
    assert f"(-> {SERVICE} :3000 [health /])" in result.output
    assert f"(-> {SERVICE} :3000 [health /] [alternate])" in result.output
    assert "(HTTP:80 -> fixed-response [default] [external])" in result.output


def test_plan_shows_the_new_tags(tmp_path, monkeypatch) -> None:
    """The flags have to be visible where a reviewer first sees the point."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plan", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "ITest plan: 12 new" in result.output
    assert (
        f"[HTTP:80 -> {TG_ALT} [priority 2 path=/*]] {LB} -> {TG_ALT}" in result.output
    )
    assert "[weight 100]" in result.output
    assert "[health /]" in result.output


def test_diagram_file_carries_the_lb_chain(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plan", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    diagram = (tmp_path / ".itest" / "diagram.mmd").read_text(encoding="utf-8")
    assert "|HTTP:80|" in diagram  # the default action
    assert "|HTTP:80 p1|" in diagram
    assert "|HTTP:80 p2|" in diagram
    assert "|:3000|" in diagram
