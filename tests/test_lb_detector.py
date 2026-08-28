"""Load balancer edges, against the committed ecs-fargate-alb state.

`tests/fixtures/aws-samples/ecs-fargate-alb.json` is sanitized real state from
the terraform-aws-modules/ecs "fargate" example: 62 managed resources, 61 of
them module-nested. Every count below was hand-derived from that file before a
line of the detector existed.

What the fixture contains
-------------------------
- 1 `aws_lb` — `module.alb.aws_lb.this[0]`, an application load balancer.
- 1 `aws_lb_listener` — `module.alb.aws_lb_listener.this["ex-http"]`, HTTP:80.
  Its single `default_action` is **not** a forward: it is a `fixed-response`
  404. Nothing is forwarded by default on this ALB.
- 2 `aws_lb_listener_rule`, both on that listener:
  - `["ex-http/production"]`, priority 1, condition `path_pattern = ["/*"]`,
    one `forward` action naming **two** target groups —
    `ex-ecs` weight 100 and `ex-ecs-alternate` weight 0. A weighted forward.
  - `["ex-http/test"]`, priority 2, condition `path_pattern = ["/*"]`, one
    `forward` action naming a single target group, `ex-ecs-alternate`
    weight 100. One group is not a split, so no weight is recorded.
- 2 `aws_lb_target_group` — `["ex-ecs"]` and `["ex-ecs-alternate"]`. Both
  `target_type = "ip"`, port 80, health check path `/`.
- 1 `aws_ecs_service` — `module.ecs_service.aws_ecs_service.this[0]`.
- 1 `aws_ecs_cluster`, 2 `aws_ecs_task_definition`.
- 0 `aws_lb_target_group_attachment`.

Which group the service feeds, and whether the other is unfed
-------------------------------------------------------------
The service's single `load_balancer` block carries:

- `target_group_arn` -> the **ex-ecs** group (`tf-80fdfdb9...`),
  `container_name = "ecsdemo-frontend"`, `container_port = 3000`;
- `advanced_configuration[0].alternate_target_group_arn` -> the
  **ex-ecs-alternate** group (`tf-af3f72a3...`), together with
  `production_listener_rule` (priority 1) and `test_listener_rule`
  (priority 2).

So the second group is **not** unfed: the same ECS service feeds it, as the
alternate group of a blue/green deployment (the service's
`deployment_configuration.strategy` is `BLUE_GREEN`). The detector must read
`advanced_configuration` as well as `target_group_arn`, or it would report a
live blue/green group as empty. `deployment_role` records which side an edge
describes, and is the discriminator that keeps the two edges' ids apart.

Expected points on this fixture: 6 lb_edge
------------------------------------------
Listener hop (4):
  1. lb -> `fixed-response`      HTTP:80  rule=default        external
  2. lb -> ex-ecs                HTTP:80  rule=priority 1     weight 100
  3. lb -> ex-ecs-alternate      HTTP:80  rule=priority 1     weight 0
  4. lb -> ex-ecs-alternate      HTTP:80  rule=priority 2     (unweighted)
Target-group hop (2):
  5. ex-ecs           -> service  :3000 health / production
  6. ex-ecs-alternate -> service  :3000 health / alternate

Plus the 6 `iam_edge` points already detected there, for 12 in total.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from itest.core.detectors.base import detect_all
from itest.core.detectors.lb_edges import LoadBalancerEdgeDetector

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "aws-samples"
    / "ecs-fargate-alb.json"
)

LB = "module.alb.aws_lb.this[0]"
LISTENER = 'module.alb.aws_lb_listener.this["ex-http"]'
RULE_PROD = 'module.alb.aws_lb_listener_rule.this["ex-http/production"]'
RULE_TEST = 'module.alb.aws_lb_listener_rule.this["ex-http/test"]'
TG_MAIN = 'module.alb.aws_lb_target_group.this["ex-ecs"]'
TG_ALT = 'module.alb.aws_lb_target_group.this["ex-ecs-alternate"]'
SERVICE = "module.ecs_service.aws_ecs_service.this[0]"
CLUSTER = "module.ecs_cluster.aws_ecs_cluster.this[0]"
TASK_DEF = "module.ecs_service.aws_ecs_task_definition.this[0]"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _edges(document: dict) -> list:
    return LoadBalancerEdgeDetector().detect(document)


def _listener_edges(document: dict) -> list:
    return [p for p in _edges(document) if p.attributes["hop"] == "listener"]


def _group_edges(document: dict) -> list:
    return [p for p in _edges(document) if p.attributes["hop"] == "target_group"]


def _walk(module: dict):
    yield from module.get("resources", []) or []
    for child in module.get("child_modules", []) or []:
        yield from _walk(child)


def _find(document: dict, address: str) -> dict:
    return next(
        r for r in _walk(document["values"]["root_module"]) if r["address"] == address
    )


def _drop(document: dict, rtype: str) -> None:
    """Remove every resource of ``rtype``, at any module depth."""

    def prune(module: dict) -> None:
        module["resources"] = [
            r for r in module.get("resources", []) or [] if r["type"] != rtype
        ]
        for child in module.get("child_modules", []) or []:
            prune(child)

    prune(document["values"]["root_module"])


# --------------------------------------------------------------------------
# Synthetic states: one small root module, built resource by resource
# --------------------------------------------------------------------------

_ARN = "arn:aws:elasticloadbalancing:eu-west-1:111111111111:"
LB_ARN = _ARN + "loadbalancer/app/synth/1111"
LISTENER_ARN = _ARN + "listener/app/synth/1111/2222"
TG_A_ARN = _ARN + "targetgroup/synth-a/aaaa"
TG_B_ARN = _ARN + "targetgroup/synth-b/bbbb"


def _resource(address: str, rtype: str, values: dict) -> dict:
    return {
        "address": address,
        "mode": "managed",
        "type": rtype,
        "name": address.rsplit(".", 1)[-1],
        "values": values,
    }


def _state(*resources: dict) -> dict:
    return {
        "format_version": "1.0",
        "values": {"root_module": {"resources": list(resources)}},
    }


def _synth_lb() -> dict:
    return _resource("aws_lb.main", "aws_lb", {"arn": LB_ARN, "id": LB_ARN})


def _synth_group(address: str, arn: str, **overrides) -> dict:
    values = {
        "arn": arn,
        "id": arn,
        "port": 80,
        "protocol": "HTTP",
        "target_type": "ip",
        "health_check": [{"path": "/healthz", "enabled": True}],
    }
    values.update(overrides)
    return _resource(address, "aws_lb_target_group", values)


def _forward_action(*groups: tuple[str, int]) -> dict:
    return {
        "type": "forward",
        "order": 1,
        "target_group_arn": "",
        "forward": [
            {"target_group": [{"arn": arn, "weight": weight} for arn, weight in groups]}
        ],
    }


def _synth_listener(*actions: dict, **overrides) -> dict:
    values = {
        "arn": LISTENER_ARN,
        "id": LISTENER_ARN,
        "load_balancer_arn": LB_ARN,
        "port": 443,
        "protocol": "HTTPS",
        "default_action": list(actions),
    }
    values.update(overrides)
    return _resource("aws_lb_listener.main", "aws_lb_listener", values)


def _synth_rule(address: str, priority: int, conditions: list, *actions: dict) -> dict:
    return _resource(
        address,
        "aws_lb_listener_rule",
        {
            "arn": _ARN + f"listener-rule/app/synth/1111/2222/{priority}",
            "listener_arn": LISTENER_ARN,
            "priority": priority,
            "condition": conditions,
            "action": list(actions),
        },
    )


# ==========================================================================
# 1. The real fixture: listener hop
# ==========================================================================


def test_fixture_yields_six_lb_edges() -> None:
    edges = _edges(_load())
    assert len(edges) == 6, [(p.source, p.target, p.attributes) for p in edges]
    assert {p.type for p in edges} == {"lb_edge"}


def test_fixture_splits_four_listener_and_two_group_edges() -> None:
    document = _load()
    assert len(_listener_edges(document)) == 4
    assert len(_group_edges(document)) == 2


def test_listener_default_action_is_the_fixed_response() -> None:
    """This ALB forwards nothing by default: its default action is a 404.

    A fixed-response is an integration too — it is what a request that matches
    no rule actually gets.
    """
    edges = [p for p in _listener_edges(_load()) if p.attributes["rule"] == "default"]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.source == LB
    assert edge.target == "fixed-response"
    assert edge.attributes["action"] == "fixed-response"
    assert edge.attributes["port"] == 80
    assert edge.attributes["protocol"] == "HTTP"
    assert edge.attributes["external"] is True
    assert "weight" not in edge.attributes
    assert edge.hcl_address == LISTENER


def test_weighted_forward_emits_one_edge_per_target_group() -> None:
    edges = [
        p for p in _listener_edges(_load()) if p.attributes["rule"].startswith("prio")
    ]
    by_key = {(p.target, p.attributes["rule"]): p for p in edges}

    prod_main = by_key[(TG_MAIN, "priority 1 path=/*")]
    prod_alt = by_key[(TG_ALT, "priority 1 path=/*")]
    assert prod_main.attributes["weight"] == 100
    assert prod_alt.attributes["weight"] == 0
    for edge in (prod_main, prod_alt):
        assert edge.source == LB
        assert edge.attributes["external"] is False
        assert edge.hcl_address == RULE_PROD


def test_single_group_forward_records_no_weight() -> None:
    """One group is not a split: a weight of 100 there says nothing."""
    edge = next(
        p
        for p in _listener_edges(_load())
        if p.attributes["rule"] == "priority 2 path=/*"
    )
    assert edge.target == TG_ALT
    assert "weight" not in edge.attributes
    assert edge.hcl_address == RULE_TEST


def test_listener_edges_resolve_the_module_nested_load_balancer() -> None:
    """Every address in this fixture is module-nested; resolution must work."""
    for edge in _listener_edges(_load()):
        assert edge.source == LB
        assert edge.source.startswith("module.")


# ==========================================================================
# 2. The real fixture: target-group hop
# ==========================================================================


def test_service_feeds_the_production_group() -> None:
    edge = next(
        p
        for p in _group_edges(_load())
        if p.attributes["deployment_role"] == "production"
    )
    assert edge.source == TG_MAIN
    assert edge.target == SERVICE
    assert edge.attributes["via"] == "ecs_service"
    assert edge.attributes["container_name"] == "ecsdemo-frontend"
    assert edge.attributes["container_port"] == 3000
    assert edge.attributes["health_check_path"] == "/"
    assert edge.attributes["target_type"] == "ip"
    assert edge.attributes["external"] is False
    assert edge.hcl_address == SERVICE


def test_the_other_group_is_fed_as_the_blue_green_alternate() -> None:
    """Derived from the state, not assumed: it is fed, so it is not "(empty)".

    `advanced_configuration.alternate_target_group_arn` on the service's
    load_balancer block names it. Reading only `target_group_arn` would have
    reported a live group as having nothing behind it.
    """
    edge = next(
        p
        for p in _group_edges(_load())
        if p.attributes["deployment_role"] == "alternate"
    )
    assert edge.source == TG_ALT
    assert edge.target == SERVICE
    assert edge.attributes["container_port"] == 3000
    assert "(empty)" not in {p.target for p in _group_edges(_load())}


def test_group_edges_carry_the_cluster_and_task_definition_context() -> None:
    """The two ECS types this detector claims but emits no edge for."""
    for edge in _group_edges(_load()):
        assert edge.attributes["cluster"] == CLUSTER
        assert edge.attributes["task_definition"] == TASK_DEF


# ==========================================================================
# 3. Identity
# ==========================================================================


def test_fixture_ids_are_unique() -> None:
    edges = _edges(_load())
    assert len({p.id for p in edges}) == len(edges)


def test_ids_are_stable_across_two_runs() -> None:
    first = sorted(p.id for p in _edges(_load()))
    second = sorted(p.id for p in _edges(_load()))
    assert first == second


def test_the_two_group_edges_differ_only_by_deployment_role() -> None:
    """Same port, same service — the discriminator is what keeps them apart."""
    edges = _group_edges(_load())
    assert len({p.id for p in edges}) == 2
    assert {p.attributes["deployment_role"] for p in edges} == {
        "production",
        "alternate",
    }


def test_id_ignores_a_condition_change() -> None:
    """Identity is the rule's priority; the condition rides in attributes."""
    document = _load()
    before = {
        p.attributes["rule"]: p.id
        for p in _listener_edges(document)
        if p.target == TG_MAIN
    }
    assert before

    changed = copy.deepcopy(document)
    rule = _find(changed, RULE_PROD)["values"]
    rule["condition"] = [{"path_pattern": [{"values": ["/api/*"]}]}]
    after = {p.target: p.id for p in _listener_edges(changed) if p.target == TG_MAIN}
    assert list(before.values()) == [after[TG_MAIN]]


def test_id_changes_with_the_listener_port() -> None:
    document = _load()
    before = sorted(p.id for p in _listener_edges(document))

    changed = copy.deepcopy(document)
    _find(changed, LISTENER)["values"]["port"] = 8080
    assert sorted(p.id for p in _listener_edges(changed)) != before


def test_ids_do_not_use_array_positions() -> None:
    """Reversing the forward's target_group list must not move any id."""
    document = _load()
    before = {p.target: p.id for p in _listener_edges(document)}

    changed = copy.deepcopy(document)
    forward = _find(changed, RULE_PROD)["values"]["action"][0]["forward"][0]
    forward["target_group"].reverse()
    after = {p.target: p.id for p in _listener_edges(changed)}
    assert before == after


# ==========================================================================
# 4. Synthetic: weighted forward
# ==========================================================================


def test_synthetic_weighted_forward_splits_into_two_edges() -> None:
    document = _state(
        _synth_lb(),
        _synth_group("aws_lb_target_group.a", TG_A_ARN),
        _synth_group("aws_lb_target_group.b", TG_B_ARN),
        _synth_listener(_forward_action((TG_A_ARN, 90), (TG_B_ARN, 10))),
    )
    listener = _listener_edges(document)
    assert len(listener) == 2
    weights = {p.target: p.attributes["weight"] for p in listener}
    assert weights == {"aws_lb_target_group.a": 90, "aws_lb_target_group.b": 10}
    for edge in listener:
        assert edge.attributes["protocol"] == "HTTPS"
        assert edge.attributes["port"] == 443
        assert edge.attributes["rule"] == "default"


# ==========================================================================
# 5. Synthetic: a listener rule with a path condition
# ==========================================================================


def test_synthetic_rule_condition_summary_joins_host_and_path() -> None:
    document = _state(
        _synth_lb(),
        _synth_group("aws_lb_target_group.a", TG_A_ARN),
        _synth_listener(_forward_action((TG_A_ARN, 100))),
        _synth_rule(
            "aws_lb_listener_rule.api",
            10,
            [
                {"host_header": [{"values": ["api.example.com"]}]},
                {"path_pattern": [{"values": ["/v1/*", "/v2/*"]}]},
            ],
            _forward_action((TG_A_ARN, 100)),
        ),
    )
    edge = next(
        p
        for p in _listener_edges(document)
        if p.hcl_address == "aws_lb_listener_rule.api"
    )
    assert edge.attributes["rule"] == (
        "priority 10 host=api.example.com path=/v1/*,/v2/*"
    )
    assert edge.source == "aws_lb.main"
    assert edge.target == "aws_lb_target_group.a"


def test_synthetic_rule_without_conditions_is_just_its_priority() -> None:
    document = _state(
        _synth_lb(),
        _synth_group("aws_lb_target_group.a", TG_A_ARN),
        _synth_listener(_forward_action((TG_A_ARN, 100))),
        _synth_rule(
            "aws_lb_listener_rule.api", 5, [], _forward_action((TG_A_ARN, 100))
        ),
    )
    rules = [p for p in _listener_edges(document) if p.attributes["rule"] != "default"]
    assert [p.attributes["rule"] for p in rules] == ["priority 5"]


def test_synthetic_redirect_action_is_an_external_edge() -> None:
    """A redirect is an integration: it is where the request actually goes."""
    document = _state(
        _synth_lb(),
        _synth_listener(
            {
                "type": "redirect",
                "order": 1,
                "redirect": [
                    {"port": "443", "protocol": "HTTPS", "status_code": "HTTP_301"}
                ],
            }
        ),
    )
    edge = _listener_edges(document)[0]
    assert edge.target == "redirect"
    assert edge.attributes["external"] is True
    assert edge.attributes["action"] == "redirect"


def test_synthetic_forward_to_an_unknown_group_is_external() -> None:
    """A target group owned by another stack: keep the ARN, flag it."""
    document = _state(
        _synth_lb(),
        _synth_listener(_forward_action((TG_B_ARN, 100))),
    )
    edge = _listener_edges(document)[0]
    assert edge.target == TG_B_ARN
    assert edge.attributes["external"] is True


# ==========================================================================
# 6. Synthetic: an empty target group
# ==========================================================================


def test_synthetic_empty_target_group_is_reported() -> None:
    """An ALB routing to a group nothing feeds is the finding, not a silence."""
    document = _state(
        _synth_lb(),
        _synth_group("aws_lb_target_group.a", TG_A_ARN),
        _synth_listener(_forward_action((TG_A_ARN, 100))),
    )
    groups = _group_edges(document)
    assert len(groups) == 1
    edge = groups[0]
    assert edge.source == "aws_lb_target_group.a"
    assert edge.target == "(empty)"
    assert edge.attributes["via"] == "none"
    assert edge.attributes["external"] is True
    assert edge.attributes["health_check_path"] == "/healthz"
    assert edge.attributes["target_type"] == "ip"
    assert edge.hcl_address == "aws_lb_target_group.a"


def test_a_fed_group_is_never_reported_empty() -> None:
    assert not [p for p in _group_edges(_load()) if p.attributes["via"] == "none"]


# ==========================================================================
# 7. Synthetic: target_group_attachment
# ==========================================================================


def test_synthetic_attachment_to_an_instance_resolves_the_instance() -> None:
    document = _state(
        _synth_lb(),
        _synth_group("aws_lb_target_group.a", TG_A_ARN, target_type="instance"),
        _resource("aws_instance.web", "aws_instance", {"id": "i-0abc", "arn": "arn:x"}),
        _resource(
            "aws_lb_target_group_attachment.web",
            "aws_lb_target_group_attachment",
            {"target_group_arn": TG_A_ARN, "target_id": "i-0abc", "port": 8080},
        ),
    )
    groups = _group_edges(document)
    assert len(groups) == 1
    edge = groups[0]
    assert edge.source == "aws_lb_target_group.a"
    assert edge.target == "aws_instance.web"
    assert edge.attributes["via"] == "attachment"
    assert edge.attributes["port"] == 8080
    assert edge.attributes["target_type"] == "instance"
    assert edge.attributes["external"] is False
    assert edge.hcl_address == "aws_lb_target_group_attachment.web"


def test_synthetic_two_attachments_are_two_edges() -> None:
    document = _state(
        _synth_lb(),
        _synth_group("aws_lb_target_group.a", TG_A_ARN, target_type="instance"),
        _resource("aws_instance.a", "aws_instance", {"id": "i-0a"}),
        _resource("aws_instance.b", "aws_instance", {"id": "i-0b"}),
        _resource(
            "aws_lb_target_group_attachment.a",
            "aws_lb_target_group_attachment",
            {"target_group_arn": TG_A_ARN, "target_id": "i-0a", "port": 80},
        ),
        _resource(
            "aws_lb_target_group_attachment.b",
            "aws_lb_target_group_attachment",
            {"target_group_arn": TG_A_ARN, "target_id": "i-0b", "port": 80},
        ),
    )
    groups = _group_edges(document)
    assert len(groups) == 2
    assert {p.target for p in groups} == {"aws_instance.a", "aws_instance.b"}
    assert len({p.id for p in groups}) == 2


def test_synthetic_attachment_to_an_unresolvable_ip_is_external() -> None:
    document = _state(
        _synth_lb(),
        _synth_group("aws_lb_target_group.a", TG_A_ARN),
        _resource(
            "aws_lb_target_group_attachment.ip",
            "aws_lb_target_group_attachment",
            {"target_group_arn": TG_A_ARN, "target_id": "10.0.1.9", "port": 3000},
        ),
    )
    edge = _group_edges(document)[0]
    assert edge.target == "10.0.1.9"
    assert edge.attributes["external"] is True


# ==========================================================================
# 8. Synthetic: NLB — a TCP listener, same shapes, no rules
# ==========================================================================


def test_synthetic_nlb_tcp_listener_has_the_same_shape() -> None:
    document = _state(
        _resource(
            "aws_lb.net",
            "aws_lb",
            {"arn": LB_ARN, "id": LB_ARN, "load_balancer_type": "network"},
        ),
        _synth_group(
            "aws_lb_target_group.redis",
            TG_A_ARN,
            port=6379,
            protocol="TCP",
            target_type="ip",
            health_check=[{"path": "", "enabled": True}],
        ),
        _synth_listener(
            {
                "type": "forward",
                "order": 1,
                "target_group_arn": TG_A_ARN,
                "forward": [],
            },
            port=6379,
            protocol="TCP",
        ),
    )
    listener = _listener_edges(document)
    assert len(listener) == 1
    edge = listener[0]
    assert edge.source == "aws_lb.net"
    assert edge.target == "aws_lb_target_group.redis"
    assert edge.attributes["protocol"] == "TCP"
    assert edge.attributes["port"] == 6379
    assert edge.attributes["rule"] == "default"
    assert "weight" not in edge.attributes
    assert edge.attributes["external"] is False


def test_synthetic_nlb_group_with_no_health_check_path() -> None:
    """A TCP health check has no path; the attribute must be absent, not ""."""
    document = _state(
        _resource("aws_lb.net", "aws_lb", {"arn": LB_ARN, "id": LB_ARN}),
        _synth_group(
            "aws_lb_target_group.redis",
            TG_A_ARN,
            health_check=[{"path": "", "protocol": "TCP"}],
        ),
    )
    edge = _group_edges(document)[0]
    assert edge.attributes["health_check_path"] is None


# ==========================================================================
# 9. Registration, the unanalyzed map, and other fixtures
# ==========================================================================


def test_lb_types_leave_the_unanalyzed_map() -> None:
    detected, unanalyzed = detect_all(_load())
    assert len(detected) == 12  # 6 iam_edge already there, plus 6 lb_edge
    assert len([p for p in detected if p.type == "lb_edge"]) == 6
    for claimed in (
        "aws_lb",
        "aws_lb_listener",
        "aws_lb_listener_rule",
        "aws_lb_target_group",
        "aws_ecs_service",
        "aws_ecs_cluster",
        "aws_ecs_task_definition",
    ):
        assert claimed not in unanalyzed, claimed
    # Not claimed: scaling and capacity are not wiring.
    for unclaimed in (
        "aws_appautoscaling_target",
        "aws_appautoscaling_policy",
        "aws_ecs_cluster_capacity_providers",
    ):
        assert unclaimed in unanalyzed, unclaimed


def test_alex_fixtures_are_untouched() -> None:
    """No load balancer in the alex states: the counts must not move."""
    alex = Path(__file__).resolve().parent / "fixtures" / "alex"
    for name, expected in (("alex-s6.json", 14), ("alex-s7.json", 12)):
        detected, _ = detect_all(json.loads((alex / name).read_text(encoding="utf-8")))
        assert len(detected) == expected, name
        assert not [p for p in detected if p.type == "lb_edge"], name


def test_a_load_balancer_with_no_listener_emits_nothing() -> None:
    """The simple-web-app plan has an aws_lb and nothing behind it."""
    document = _state(_synth_lb())
    assert _edges(document) == []


def test_listener_without_a_resolvable_load_balancer_keeps_the_arn() -> None:
    document = _state(
        _synth_group("aws_lb_target_group.a", TG_A_ARN),
        _synth_listener(_forward_action((TG_A_ARN, 100))),
    )
    edge = _listener_edges(document)[0]
    assert edge.source == LB_ARN


def test_rule_on_an_unknown_listener_emits_nothing() -> None:
    """Without the listener there is no port or protocol — only a guess."""
    document = _state(
        _synth_lb(),
        _synth_group("aws_lb_target_group.a", TG_A_ARN),
        _synth_rule(
            "aws_lb_listener_rule.api", 5, [], _forward_action((TG_A_ARN, 100))
        ),
    )
    assert _listener_edges(document) == []


def test_dropping_the_service_makes_both_groups_empty() -> None:
    document = _load()
    _drop(document, "aws_ecs_service")
    groups = _group_edges(document)
    assert len(groups) == 2
    assert {p.target for p in groups} == {"(empty)"}
    assert all(p.attributes["via"] == "none" for p in groups)


# ==========================================================================
# 10. The manifest type literal, which the detector cannot construct without
# ==========================================================================


def test_a_synthetic_point_validates_against_the_manifest_schema() -> None:
    """The type literal has to admit lb_edge (task 2 wires it in)."""
    from itest.core.manifest import IntegrationPoint

    now = datetime.now(UTC)
    point = IntegrationPoint(
        id="0123456789ab",
        type="lb_edge",
        source="aws_lb.main",
        target="aws_lb_target_group.a",
        attributes={"hop": "listener", "port": 443, "protocol": "HTTPS"},
        hcl_address="aws_lb_listener.main",
        first_seen=now,
        last_seen=now,
    )
    assert point.type == "lb_edge"
