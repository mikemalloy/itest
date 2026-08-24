"""Security-group edge detector.

Emits one ``sg_edge`` integration point per security-group rule that lets one
party reach another. The rule direction is treated asymmetrically, which is
what keeps the output meaningful:

- **ingress** rules always produce an edge (inbound reachability defines who
  can talk to a resource — a public CIDR entry point is exactly as interesting
  as an SG-to-SG one).
- **egress** rules produce an edge only when they target *another security
  group*. Broad "allow all outbound to 0.0.0.0/0" rules are not integration
  points — they say "has internet access", not "A integrates with B".

Both inline rules (`ingress`/`egress` blocks on ``aws_security_group``) and
standalone ``aws_security_group_rule`` resources are handled. Point IDs are a
deterministic hash of the edge's content, never array positions, so they stay
stable across runs.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from itest.core.manifest import IntegrationPoint


def _ports(from_port, to_port) -> str:
    """Render a port range: ``"443"`` when single, ``"8000-8100"`` otherwise."""
    if from_port == to_port:
        return str(from_port)
    return f"{from_port}-{to_port}"


def _point_id(
    source: str, target: str, protocol: str, ports: str, direction: str
) -> str:
    """Deterministic short id derived from edge content."""
    raw = "|".join(["sg_edge", source, target, protocol, ports, direction])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _make_point(
    source: str, target: str, protocol: str, ports: str, direction: str, hcl: str
) -> IntegrationPoint:
    now = datetime.now(timezone.utc)
    return IntegrationPoint(
        id=_point_id(source, target, protocol, ports, direction),
        type="sg_edge",
        source=source,
        target=target,
        attributes={"protocol": protocol, "ports": ports, "direction": direction},
        hcl_address=hcl,
        origin="detected",
        first_seen=now,
        last_seen=now,
    )


class SecurityGroupEdgeDetector:
    """Detect security-group reachability edges."""

    handled_types = {"aws_security_group", "aws_security_group_rule"}

    def detect(self, plan_json: dict) -> list[IntegrationPoint]:
        # Local import avoids a circular import at module load time.
        from itest.core.detectors.base import iter_resources

        resources = list(iter_resources(plan_json))
        id_to_address = self._sg_id_map(resources)

        points: list[IntegrationPoint] = []
        for resource in resources:
            if resource.get("mode") != "managed":
                continue
            rtype = resource.get("type")
            if rtype == "aws_security_group":
                points.extend(self._inline_rules(resource, id_to_address))
            elif rtype == "aws_security_group_rule":
                points.extend(self._standalone_rule(resource, id_to_address))
        return points

    @staticmethod
    def _sg_id_map(resources: list[dict]) -> dict[str, str]:
        """Map each security group's real id back to its HCL address."""
        mapping: dict[str, str] = {}
        for resource in resources:
            if resource.get("type") != "aws_security_group":
                continue
            sg_id = (resource.get("values") or {}).get("id")
            if sg_id:
                mapping[sg_id] = resource["address"]
        return mapping

    def _inline_rules(
        self, resource: dict, id_to_address: dict[str, str]
    ) -> list[IntegrationPoint]:
        address = resource["address"]
        values = resource.get("values") or {}
        points: list[IntegrationPoint] = []

        for i, rule in enumerate(values.get("ingress") or []):
            for source in self._rule_sources(rule, id_to_address):
                points.append(
                    _make_point(
                        source=source,
                        target=address,
                        protocol=rule.get("protocol", ""),
                        ports=_ports(rule.get("from_port"), rule.get("to_port")),
                        direction="ingress",
                        hcl=f"{address}.ingress[{i}]",
                    )
                )

        for i, rule in enumerate(values.get("egress") or []):
            # Egress only counts when it points at another security group.
            for target in self._referenced_sgs(rule, id_to_address):
                points.append(
                    _make_point(
                        source=address,
                        target=target,
                        protocol=rule.get("protocol", ""),
                        ports=_ports(rule.get("from_port"), rule.get("to_port")),
                        direction="egress",
                        hcl=f"{address}.egress[{i}]",
                    )
                )
        return points

    def _standalone_rule(
        self, resource: dict, id_to_address: dict[str, str]
    ) -> list[IntegrationPoint]:
        values = resource.get("values") or {}
        address = resource["address"]
        rtype = values.get("type", "ingress")
        attached = values.get("security_group_id")
        attached_addr = id_to_address.get(attached, attached)
        protocol = values.get("protocol", "")
        ports = _ports(values.get("from_port"), values.get("to_port"))

        src_sg = values.get("source_security_group_id")
        cidrs = values.get("cidr_blocks") or []
        points: list[IntegrationPoint] = []

        if rtype == "ingress":
            sources: list[str] = []
            if src_sg:
                sources.append(id_to_address.get(src_sg, src_sg))
            sources.extend(cidrs)
            for source in sources:
                points.append(
                    _make_point(
                        source=source,
                        target=attached_addr,
                        protocol=protocol,
                        ports=ports,
                        direction="ingress",
                        hcl=address,
                    )
                )
        else:  # egress: only SG-to-SG, mirroring the inline rule policy.
            if src_sg:
                points.append(
                    _make_point(
                        source=attached_addr,
                        target=id_to_address.get(src_sg, src_sg),
                        protocol=protocol,
                        ports=ports,
                        direction="egress",
                        hcl=address,
                    )
                )
        return points

    @staticmethod
    def _rule_sources(rule: dict, id_to_address: dict[str, str]) -> list[str]:
        """All sources for an inline ingress block: CIDRs and referenced SGs."""
        sources = list(rule.get("cidr_blocks") or [])
        for sg_id in rule.get("security_groups") or []:
            sources.append(id_to_address.get(sg_id, sg_id))
        return sources

    @staticmethod
    def _referenced_sgs(rule: dict, id_to_address: dict[str, str]) -> list[str]:
        """Only the security groups an inline block references (no CIDRs)."""
        return [
            id_to_address.get(sg_id, sg_id)
            for sg_id in rule.get("security_groups") or []
        ]
