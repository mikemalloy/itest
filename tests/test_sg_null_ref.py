"""F7 regression: a standalone SG rule with a null required ref must not crash.

In `terraform plan -json`, a `security_group_id` that is known-after-apply is
null. The detector joined it into the point-id with "|".join([... None ...]) and
raised TypeError. The README says "state beats plan," but plan must degrade, not
crash: a rule whose anchoring security group is unresolved is skipped.
"""

from __future__ import annotations

from itest.core.detectors.sg_edges import SecurityGroupEdgeDetector


def _plan(resources: list[dict]) -> dict:
    return {"planned_values": {"root_module": {"resources": resources}}}


def test_null_security_group_id_is_skipped_not_a_crash() -> None:
    plan = _plan(
        [
            {
                "address": "aws_security_group_rule.web_ingress",
                "mode": "managed",
                "type": "aws_security_group_rule",
                "name": "web_ingress",
                "values": {
                    "type": "ingress",
                    "security_group_id": None,  # known-after-apply in a plan
                    "cidr_blocks": ["0.0.0.0/0"],
                    "protocol": "tcp",
                    "from_port": 443,
                    "to_port": 443,
                },
            }
        ]
    )
    # No TypeError, and the unanchored rule produces no edge.
    assert SecurityGroupEdgeDetector().detect(plan) == []


def test_a_resolvable_rule_still_emits_beside_a_null_one() -> None:
    plan = _plan(
        [
            {
                "address": "aws_security_group.db",
                "mode": "managed",
                "type": "aws_security_group",
                "name": "db",
                "values": {"id": "sg-db"},
            },
            {
                "address": "aws_security_group_rule.null_ref",
                "mode": "managed",
                "type": "aws_security_group_rule",
                "name": "null_ref",
                "values": {
                    "type": "ingress",
                    "security_group_id": None,
                    "cidr_blocks": ["10.0.0.0/8"],
                    "protocol": "tcp",
                    "from_port": 5432,
                    "to_port": 5432,
                },
            },
            {
                "address": "aws_security_group_rule.good",
                "mode": "managed",
                "type": "aws_security_group_rule",
                "name": "good",
                "values": {
                    "type": "ingress",
                    "security_group_id": "sg-db",
                    "cidr_blocks": ["10.0.0.0/8"],
                    "protocol": "tcp",
                    "from_port": 5432,
                    "to_port": 5432,
                },
            },
        ]
    )
    points = SecurityGroupEdgeDetector().detect(plan)
    # The null-ref rule is skipped; the resolvable one still emits its edge.
    assert len(points) == 1
    assert points[0].target == "aws_security_group.db"
    assert points[0].source == "10.0.0.0/8"
