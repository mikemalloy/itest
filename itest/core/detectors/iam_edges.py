"""IAM edge detector.

Emits one ``iam_edge`` integration point per (role, granted resource) pair:
the edge runs **role -> resource**, mirroring ``sg_edge``'s source/target
shape, and the granted actions ride along as attributes rather than becoming
edges of their own. This keeps the graph legible and the schema symmetric.

Sources parsed:

- ``aws_iam_role`` ``inline_policy`` blocks (policy is a JSON string).
- ``aws_iam_role_policy`` resources. Terraform surfaces the same policy both
  ways when it is managed standalone, so a standalone policy suppresses the
  inline copy with the same (role, policy name).
- ``aws_iam_role_policy_attachment`` — emits ``role -> managed policy ARN``
  with ``managed: true`` and actions left ``<unresolved>``; AWS-managed
  ``*FullAccess`` policies are flagged ``broad_managed_policy``.

Target resolution: a statement resource ARN that matches the ``arn`` of a
resource present in the same plan/state resolves to that resource's HCL
address. Anything else stays as the ARN and is flagged ``external: true`` —
this is how cross-stack references (stage 6 granting access to stage 5's
cluster) become visible instead of vanishing.

Point IDs are a deterministic hash of (type, source, target, sorted actions,
effect) — never array positions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from itest.core.manifest import IntegrationPoint

UNRESOLVED = "<unresolved>"
AWS_MANAGED_PREFIX = "arn:aws:iam::aws:policy/"


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _point_id(source: str, target: str, actions: list[str], effect: str) -> str:
    raw = "|".join(["iam_edge", source, target, ",".join(actions), effect])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _make_point(
    source: str, target: str, attributes: dict, hcl: str
) -> IntegrationPoint:
    now = datetime.now(UTC)
    return IntegrationPoint(
        id=_point_id(source, target, attributes["actions"], attributes["effect"]),
        type="iam_edge",
        source=source,
        target=target,
        attributes=attributes,
        hcl_address=hcl,
        origin="detected",
        first_seen=now,
        last_seen=now,
    )


def _is_broad_managed(policy_arn: str) -> bool:
    return policy_arn.startswith(AWS_MANAGED_PREFIX) and policy_arn.endswith(
        "FullAccess"
    )


class IamEdgeDetector:
    """Detect IAM grant edges from roles to the resources they may touch."""

    handled_types = {
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
    }

    def detect(self, plan_json: dict) -> list[IntegrationPoint]:
        from itest.core.detectors.base import iter_resources

        resources = [r for r in iter_resources(plan_json) if r.get("mode") == "managed"]
        arn_to_address = self._arn_map(resources)
        role_name_to_address = self._role_map(resources)

        # (role address, policy name) pairs covered by standalone policies.
        standalone: set[tuple[str, str]] = set()
        for resource in resources:
            if resource.get("type") != "aws_iam_role_policy":
                continue
            values = resource.get("values") or {}
            role_addr = role_name_to_address.get(values.get("role"), values.get("role"))
            standalone.add((role_addr, values.get("name")))

        points: list[IntegrationPoint] = []
        seen: set[str] = set()

        def emit(point: IntegrationPoint) -> None:
            if point.id in seen:
                return
            seen.add(point.id)
            points.append(point)

        for resource in resources:
            rtype = resource.get("type")
            values = resource.get("values") or {}
            address = resource["address"]

            if rtype == "aws_iam_role":
                for block in values.get("inline_policy") or []:
                    name = block.get("name")
                    if (address, name) in standalone:
                        continue
                    hcl = f"{address}.inline_policy[{name}]"
                    for point in self._policy_edges(
                        address, block.get("policy"), hcl, arn_to_address
                    ):
                        emit(point)

            elif rtype == "aws_iam_role_policy":
                role_addr = role_name_to_address.get(
                    values.get("role"), values.get("role")
                )
                for point in self._policy_edges(
                    role_addr, values.get("policy"), address, arn_to_address
                ):
                    emit(point)

            elif rtype == "aws_iam_role_policy_attachment":
                role_addr = role_name_to_address.get(
                    values.get("role"), values.get("role")
                )
                policy_arn = values.get("policy_arn") or ""
                if not role_addr or not policy_arn:
                    continue
                emit(
                    _make_point(
                        source=role_addr,
                        target=policy_arn,
                        attributes={
                            "actions": [UNRESOLVED],
                            "effect": "Allow",
                            "managed": True,
                            "broad_managed_policy": _is_broad_managed(policy_arn),
                            "wildcard_action": False,
                            "wildcard_resource": False,
                            "external": False,
                        },
                        hcl=address,
                    )
                )
        return points

    @staticmethod
    def _arn_map(resources: list[dict]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for resource in resources:
            arn = (resource.get("values") or {}).get("arn")
            # First writer wins so a secret beats its secret_version, which
            # shares the same ARN and appears later in state.
            if isinstance(arn, str) and arn and arn not in mapping:
                mapping[arn] = resource["address"]
        return mapping

    @staticmethod
    def _role_map(resources: list[dict]) -> dict[str, str]:
        """Map role names (what policies/attachments reference) to addresses."""
        mapping: dict[str, str] = {}
        for resource in resources:
            if resource.get("type") != "aws_iam_role":
                continue
            values = resource.get("values") or {}
            for key in ("name", "id", "arn"):
                value = values.get(key)
                if isinstance(value, str) and value:
                    mapping[value] = resource["address"]
        return mapping

    @staticmethod
    def _policy_edges(
        role_addr: str | None,
        policy: object,
        hcl: str,
        arn_to_address: dict[str, str],
    ) -> list[IntegrationPoint]:
        if not role_addr or not policy:
            return []
        document = policy
        if isinstance(policy, str):
            try:
                document = json.loads(policy)
            except json.JSONDecodeError:
                return []
        if not isinstance(document, dict):
            return []

        points: list[IntegrationPoint] = []
        for statement in _as_list(document.get("Statement")):
            if not isinstance(statement, dict):
                continue
            effect = statement.get("Effect", "Allow")
            actions = sorted(str(a) for a in _as_list(statement.get("Action")))
            wildcard_action = any("*" in a for a in actions)
            for resource_arn in _as_list(statement.get("Resource")):
                resource_arn = str(resource_arn)
                resolved = arn_to_address.get(resource_arn)
                points.append(
                    _make_point(
                        source=role_addr,
                        target=resolved or resource_arn,
                        attributes={
                            "actions": actions,
                            "effect": effect,
                            "wildcard_action": wildcard_action,
                            "wildcard_resource": "*" in resource_arn,
                            "external": resolved is None,
                            "managed": False,
                        },
                        hcl=hcl,
                    )
                )
        return points
