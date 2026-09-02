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
- Policy bindings: ``aws_iam_role_policy_attachment``,
  ``aws_iam_policy_attachment`` (plural, with a ``roles`` list), and a role's
  own ``managed_policy_arns``. What a binding emits depends on whether the
  policy can be read:

  - **Customer-managed and present in this document** — the ``aws_iam_policy``
    resource's ``policy`` JSON is parsed and emits ordinary edges, exactly as
    an inline policy does, plus ``via_policy`` naming where the grant lives.
    An opaque managed edge is *not* also emitted; the resolved edges are the
    truth. AWS's own serverless-patterns samples grant this way, and treating
    them as opaque threw away everything ITest could say about them.
  - **Otherwise** — one edge with ``managed: true`` and actions left
    ``<unresolved>``. AWS-managed ``*FullAccess`` policies are flagged
    ``broad_managed_policy``; a customer policy another stack owns also gets
    ``external: true``.

  When the same binding is declared more than one way, the more specific
  declaring resource wins the recorded ``hcl_address`` — an attachment
  resource over a role's arn list — and dedupe by point id keeps one edge.
  A policy attached to nothing emits nothing: a policy is not an integration
  until something holds it.

Target resolution: a statement resource ARN that matches the ``arn`` of a
resource present in the same plan/state resolves to that resource's HCL
address. Anything else stays as the ARN and is flagged ``external: true`` —
this is how cross-stack references (stage 6 granting access to stage 5's
cluster) become visible instead of vanishing.

Point IDs are a deterministic hash of (type, source, target, sorted actions,
effect) — never array positions — extended with ``via_policy`` when set, so
moving a statement into a customer-managed policy reads as a change rather
than as the same point.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from itest.core.manifest import IntegrationPoint

UNRESOLVED = "<unresolved>"
AWS_MANAGED_PREFIX = "arn:aws:iam::aws:policy/"

#: An S3 object-space ARN: the bucket ARN, then a key path. A grant on
#: ``arn:aws:s3:::bucket/*`` is over the bucket's objects, so it resolves to the
#: bucket resource (tagged object-scope) rather than reading external.
_S3_OBJECT_ARN = re.compile(r"^(arn:aws:s3:::[^/]+)/.+$")


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _point_id(
    source: str,
    target: str,
    actions: list[str],
    effect: str,
    via_policy: str | None = None,
    object_scope: bool = False,
) -> str:
    parts = ["iam_edge", source, target, ",".join(actions), effect]
    # Appended only when present, so ids of edges that predate customer-managed
    # resolution are unchanged — and so the same grant reached through a policy
    # is a different point from the same grant written inline.
    if via_policy:
        parts.append(via_policy)
    # A grant on the bucket's objects (bucket/*) resolves to the same bucket
    # address as a grant on the bucket itself, but it is a distinct grant.
    # Appended only for the object-scope edge, so every other id is unchanged.
    if object_scope:
        parts.append("object_scope")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _make_point(
    source: str, target: str, attributes: dict, hcl: str
) -> IntegrationPoint:
    now = datetime.now(UTC)
    return IntegrationPoint(
        id=_point_id(
            source,
            target,
            attributes["actions"],
            attributes["effect"],
            attributes.get("via_policy"),
            attributes.get("object_scope", False),
        ),
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
        "aws_iam_policy",
        "aws_iam_policy_attachment",
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

        # Policy bindings, in precedence order: an explicit attachment resource
        # is a more precise home for an edge than the role's own arn list, so
        # when both declare the same binding the attachment's address is the
        # one recorded.
        customer_policies = self._customer_policies(resources)
        for role_addr, policy_arn, hcl in self._bindings(
            resources, role_name_to_address
        ):
            resolved_policy = customer_policies.get(policy_arn)
            if resolved_policy is not None:
                policy_address, document = resolved_policy
                for point in self._policy_edges(
                    role_addr,
                    document,
                    hcl,
                    arn_to_address,
                    via_policy=policy_address,
                ):
                    emit(point)
                continue

            # Nothing to read: an AWS-managed policy, or a customer policy
            # another stack owns. Only the latter is external.
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
                        "external": not policy_arn.startswith(AWS_MANAGED_PREFIX),
                    },
                    hcl=hcl,
                )
            )
        return points

    @staticmethod
    def _customer_policies(resources: list[dict]) -> dict[str, tuple[str, object]]:
        """Map a customer-managed policy ARN to (its address, its document).

        Only policies declared in this document appear, which is exactly the
        set whose grants can be read rather than guessed at.
        """
        mapping: dict[str, tuple[str, object]] = {}
        for resource in resources:
            if resource.get("type") != "aws_iam_policy":
                continue
            values = resource.get("values") or {}
            arn = values.get("arn")
            if isinstance(arn, str) and arn and arn not in mapping:
                mapping[arn] = (resource["address"], values.get("policy"))
        return mapping

    @staticmethod
    def _bindings(
        resources: list[dict], role_name_to_address: dict[str, str]
    ) -> list[tuple[str, str, str]]:
        """Every (role address, policy ARN, declaring HCL address) binding.

        Ordered by how specific the declaring resource is, because emit()
        keeps the first point for a given id: a dedicated attachment resource
        names the binding better than a list on the role.
        """
        bindings: list[tuple[str, str, str]] = []

        def add(role: object, policy_arn: object, hcl: str) -> None:
            role_addr = role_name_to_address.get(role, role)  # type: ignore[arg-type]
            if isinstance(role_addr, str) and isinstance(policy_arn, str):
                if role_addr and policy_arn:
                    bindings.append((role_addr, policy_arn, hcl))

        for resource in resources:
            if resource.get("type") == "aws_iam_role_policy_attachment":
                values = resource.get("values") or {}
                add(values.get("role"), values.get("policy_arn"), resource["address"])

        for resource in resources:
            if resource.get("type") == "aws_iam_policy_attachment":
                values = resource.get("values") or {}
                for role in _as_list(values.get("roles")):
                    add(role, values.get("policy_arn"), resource["address"])

        for resource in resources:
            if resource.get("type") == "aws_iam_role":
                values = resource.get("values") or {}
                for policy_arn in _as_list(values.get("managed_policy_arns")):
                    add(resource["address"], policy_arn, resource["address"])

        return bindings

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
        via_policy: str | None = None,
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
                object_scope = False
                if resolved is None:
                    match = _S3_OBJECT_ARN.match(resource_arn)
                    if match:
                        bucket_address = arn_to_address.get(match.group(1))
                        if bucket_address is not None:
                            resolved = bucket_address
                            object_scope = True
                attributes = {
                    "actions": actions,
                    "effect": effect,
                    "wildcard_action": wildcard_action,
                    "wildcard_resource": "*" in resource_arn,
                    "external": resolved is None,
                    "managed": False,
                }
                # Appended only for a resolved object-space grant, so every other
                # edge's attributes — and its id — are unchanged.
                if object_scope:
                    attributes["object_scope"] = True
                if via_policy:
                    attributes["via_policy"] = via_policy
                points.append(
                    _make_point(
                        source=role_addr,
                        target=resolved or resource_arn,
                        attributes=attributes,
                        hcl=hcl,
                    )
                )
        return points
