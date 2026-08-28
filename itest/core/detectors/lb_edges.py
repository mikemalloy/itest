"""Load balancer edge detector: listener -> target group -> what serves it.

An ALB or NLB is a chain, and a release owner needs each link checked on its
own: a listener can forward to a target group that nothing has registered
into, and an ECS service can be wired to a group the listener never names.
One `lb_edge` type therefore covers two *hops*, distinguished by the ``hop``
attribute, so each is independently verifiable:

- ``hop: "listener"`` — source is the ``aws_lb`` (resolved from the
  listener's ``load_balancer_arn``), target is the ``aws_lb_target_group``
  the action forwards to. Both ``aws_lb_listener`` default actions and
  ``aws_lb_listener_rule`` actions produce these; ``rule`` records which
  (``default``, or the rule's priority plus its host/path conditions).
  A *weighted* forward — more than one target group in one action — emits one
  edge per group and records ``weight``; a single-group forward records none,
  because a weight of 100 on the only group says nothing. A non-forward
  action (``redirect``, ``fixed-response``) targets the action type itself
  and is flagged ``external``: a redirect is where the request actually
  goes, so it is an integration too.

- ``hop: "target_group"`` — source is the ``aws_lb_target_group``, target is
  what feeds it. An ``aws_ecs_service`` whose ``load_balancer`` block names
  the group feeds it, and so does each ``aws_lb_target_group_attachment``
  pointed at it. A group nothing feeds emits an edge to ``(empty)`` with
  ``external: true`` — an ALB routing to an empty target group is exactly the
  finding this detector exists to surface, and silence would hide it.

**Blue/green.** A service's ``load_balancer`` block can name a second group
in ``advanced_configuration.alternate_target_group_arn``. That group is fed,
just not by the production listener rule, so it must not be reported empty.
``deployment_role`` (``production`` / ``alternate``) records which side an
edge describes and is what keeps the two edges' ids distinct.

**Cluster and task definition.** ``aws_ecs_cluster`` and
``aws_ecs_task_definition`` are claimed but emit no edge of their own: they
are context, resolved onto the service's edges as ``cluster`` and
``task_definition`` so a check can name them without re-resolving ARNs. They
are not integration points — a cluster does not route to anything.

Everything resolves through ARNs against the resources in the same document,
so module-nested addresses work unchanged. An ARN with nothing behind it in
this state is kept verbatim and flagged ``external``: it belongs to another
stack.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from itest.core.manifest import IntegrationPoint

#: What a target group with nothing behind it points at. Not an address, and
#: deliberately not a valid one — it must never look resolvable.
EMPTY_TARGET = "(empty)"


def _point_id(source: str, target: str, port: str, discriminator: str) -> str:
    """Content-derived id: the two ends, the port, and which rule made it.

    Never an array position: reversing the target groups inside one forward
    action, or reordering rules in state, must not move a single id. The
    discriminator is the rule's priority (or ``default``), not its conditions
    — retargeting a path is a change to the point, not a different point.
    """
    parts = ["lb_edge", source, target, port, discriminator]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _make_point(
    source: str,
    target: str,
    attributes: dict,
    hcl: str,
    port: str,
    discriminator: str,
) -> IntegrationPoint:
    now = datetime.now(UTC)
    return IntegrationPoint(
        id=_point_id(source, target, port, discriminator),
        type="lb_edge",
        source=source,
        target=target,
        attributes=attributes,
        hcl_address=hcl,
        origin="detected",
        first_seen=now,
        last_seen=now,
    )


def _condition_summary(conditions) -> str:
    """``host=api.example.com path=/v1/*,/v2/*`` — the routing predicate.

    Host and path are what a reviewer routes on; the remaining condition
    types (headers, methods, query strings, source IPs) are refinements and
    stay out of the one-line summary.
    """
    hosts: list[str] = []
    paths: list[str] = []
    for condition in conditions or []:
        if not isinstance(condition, dict):
            continue
        for block in condition.get("host_header") or []:
            hosts.extend(block.get("values") or [])
        for block in condition.get("path_pattern") or []:
            paths.extend(block.get("values") or [])
    parts = []
    if hosts:
        parts.append("host=" + ",".join(hosts))
    if paths:
        parts.append("path=" + ",".join(paths))
    return " ".join(parts)


def _health_check_path(group_values: dict) -> str | None:
    """The group's HTTP health check path, or None for a TCP check."""
    for block in group_values.get("health_check") or []:
        path = block.get("path")
        if isinstance(path, str) and path:
            return path
    return None


class LoadBalancerEdgeDetector:
    """Detect listener -> target group -> service/target edges."""

    handled_types = {
        "aws_lb",
        "aws_lb_listener",
        "aws_lb_listener_rule",
        "aws_lb_target_group",
        "aws_lb_target_group_attachment",
        "aws_ecs_service",
        # Context, not edges: resolved onto the service's edges below.
        "aws_ecs_cluster",
        "aws_ecs_task_definition",
    }

    def detect(self, plan_json: dict) -> list[IntegrationPoint]:
        from itest.core.detectors.base import iter_resources

        resources = [r for r in iter_resources(plan_json) if r.get("mode") == "managed"]

        load_balancers = self._by_arn(resources, "aws_lb")
        groups = self._by_arn(resources, "aws_lb_target_group")
        listeners = self._by_arn(resources, "aws_lb_listener")

        points: list[IntegrationPoint] = []
        seen: set[str] = set()

        def emit(point: IntegrationPoint) -> None:
            if point.id in seen:
                return
            seen.add(point.id)
            points.append(point)

        for point in self._listener_edges(resources, load_balancers, listeners, groups):
            emit(point)
        for point in self._group_edges(resources, groups):
            emit(point)
        return points

    # -- hop 1: listener -> target group -----------------------------------

    def _listener_edges(
        self,
        resources: list[dict],
        load_balancers: dict[str, dict],
        listeners: dict[str, dict],
        groups: dict[str, dict],
    ) -> list[IntegrationPoint]:
        found: list[IntegrationPoint] = []

        for resource in self._of_type(resources, "aws_lb_listener"):
            values = resource["values"]
            source = self._source_for_listener(values, load_balancers)
            found.extend(
                self._edges_for_actions(
                    actions=values.get("default_action") or [],
                    source=source,
                    port=values.get("port"),
                    protocol=values.get("protocol"),
                    rule="default",
                    discriminator="default",
                    hcl=resource["address"],
                    groups=groups,
                )
            )

        for resource in self._of_type(resources, "aws_lb_listener_rule"):
            values = resource["values"]
            listener = listeners.get(values.get("listener_arn"))
            if listener is None:
                # Without the listener there is no port or protocol to record
                # — only a guess, which is not an edge.
                continue
            listener_values = listener["values"]
            source = self._source_for_listener(listener_values, load_balancers)
            priority = values.get("priority")
            conditions = _condition_summary(values.get("condition"))
            rule = f"priority {priority}"
            if conditions:
                rule = f"{rule} {conditions}"
            found.extend(
                self._edges_for_actions(
                    actions=values.get("action") or [],
                    source=source,
                    port=listener_values.get("port"),
                    protocol=listener_values.get("protocol"),
                    rule=rule,
                    discriminator=f"priority {priority}",
                    hcl=resource["address"],
                    groups=groups,
                )
            )
        return found

    def _edges_for_actions(
        self,
        actions: list,
        source: str,
        port,
        protocol,
        rule: str,
        discriminator: str,
        hcl: str,
        groups: dict[str, dict],
    ) -> list[IntegrationPoint]:
        found: list[IntegrationPoint] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            for target, weight, external in self._action_targets(action, groups):
                attributes = {
                    "hop": "listener",
                    "port": port,
                    "protocol": protocol,
                    "rule": rule,
                    "action": action.get("type"),
                    "external": external,
                }
                if weight is not None:
                    attributes["weight"] = weight
                found.append(
                    _make_point(
                        source=source,
                        target=target,
                        attributes=attributes,
                        hcl=hcl,
                        port=f"{protocol}:{port}",
                        discriminator=discriminator,
                    )
                )
        return found

    @staticmethod
    def _action_targets(action: dict, groups: dict[str, dict]) -> list[tuple]:
        """Return ``(target, weight, external)`` for one listener action."""
        if action.get("type") != "forward":
            # redirect, fixed-response, authenticate-*: nothing in this state
            # sits behind them, and the action type is the honest far end.
            kind = action.get("type")
            return [(str(kind) if kind else "unknown", None, True)]

        entries: list[dict] = []
        for block in action.get("forward") or []:
            entries.extend(block.get("target_group") or [])

        if entries:
            # More than one group is a genuine split; one is not, so its
            # weight is noise and is left out.
            weighted = len(entries) > 1
            resolved = []
            for entry in entries:
                target, external = LoadBalancerEdgeDetector._resolve_group(
                    entry.get("arn"), groups
                )
                resolved.append(
                    (target, entry.get("weight") if weighted else None, external)
                )
            return resolved

        # The short form: a single `target_group_arn` on the action itself.
        arn = action.get("target_group_arn")
        if not arn:
            return []
        target, external = LoadBalancerEdgeDetector._resolve_group(arn, groups)
        return [(target, None, external)]

    @staticmethod
    def _resolve_group(arn, groups: dict[str, dict]) -> tuple[str, bool]:
        if not isinstance(arn, str) or not arn:
            return "unknown", True
        group = groups.get(arn)
        return (group["address"], False) if group else (arn, True)

    @staticmethod
    def _source_for_listener(values: dict, load_balancers: dict[str, dict]) -> str:
        """The load balancer address, or its ARN when it is in another stack."""
        arn = values.get("load_balancer_arn")
        balancer = load_balancers.get(arn) if isinstance(arn, str) else None
        if balancer is not None:
            return balancer["address"]
        return arn if isinstance(arn, str) and arn else "unknown"

    # -- hop 2: target group -> what feeds it ------------------------------

    def _group_edges(
        self, resources: list[dict], groups: dict[str, dict]
    ) -> list[IntegrationPoint]:
        services = self._of_type(resources, "aws_ecs_service")
        attachments = self._of_type(resources, "aws_lb_target_group_attachment")
        clusters = self._by_arn(resources, "aws_ecs_cluster")
        task_definitions = self._task_definition_map(resources)
        registrable = self._registrable_targets(resources)

        found: list[IntegrationPoint] = []
        for resource in self._of_type(resources, "aws_lb_target_group"):
            values = resource["values"]
            address = resource["address"]
            arns = {
                v
                for v in (values.get("arn"), values.get("id"))
                if isinstance(v, str) and v
            }
            base = {
                "hop": "target_group",
                "health_check_path": _health_check_path(values),
                "target_type": values.get("target_type"),
            }

            edges = self._service_edges(
                services, arns, address, base, clusters, task_definitions
            )
            edges += self._attachment_edges(
                attachments, arns, address, base, registrable
            )

            if not edges:
                # The finding, stated rather than skipped.
                edges = [
                    _make_point(
                        source=address,
                        target=EMPTY_TARGET,
                        attributes={**base, "via": "none", "external": True},
                        hcl=address,
                        port="",
                        discriminator="empty",
                    )
                ]
            found.extend(edges)
        return found

    @staticmethod
    def _service_edges(
        services: list[dict],
        arns: set[str],
        address: str,
        base: dict,
        clusters: dict[str, dict],
        task_definitions: dict[str, str],
    ) -> list[IntegrationPoint]:
        found: list[IntegrationPoint] = []
        for service in services:
            values = service["values"]
            cluster = clusters.get(values.get("cluster"))
            context = {
                "cluster": cluster["address"] if cluster else values.get("cluster"),
                "task_definition": task_definitions.get(
                    values.get("task_definition"), values.get("task_definition")
                ),
            }
            for block in values.get("load_balancer") or []:
                if not isinstance(block, dict):
                    continue
                roles: list[str] = []
                if block.get("target_group_arn") in arns:
                    roles.append("production")
                for advanced in block.get("advanced_configuration") or []:
                    if advanced.get("alternate_target_group_arn") in arns:
                        roles.append("alternate")
                for role in roles:
                    port = block.get("container_port")
                    found.append(
                        _make_point(
                            source=address,
                            target=service["address"],
                            attributes={
                                **base,
                                "via": "ecs_service",
                                "container_name": block.get("container_name"),
                                "container_port": port,
                                "deployment_role": role,
                                **context,
                                "external": False,
                            },
                            hcl=service["address"],
                            port=str(port),
                            discriminator=f"ecs:{role}",
                        )
                    )
        return found

    @staticmethod
    def _attachment_edges(
        attachments: list[dict],
        arns: set[str],
        address: str,
        base: dict,
        registrable: dict[str, str],
    ) -> list[IntegrationPoint]:
        found: list[IntegrationPoint] = []
        for attachment in attachments:
            values = attachment["values"]
            if values.get("target_group_arn") not in arns:
                continue
            target_id = values.get("target_id")
            resolved = (
                registrable.get(target_id) if isinstance(target_id, str) else None
            )
            # An IP literal or an id from another stack resolves to nothing
            # here: keep it verbatim rather than inventing an address.
            target = resolved or (
                target_id if isinstance(target_id, str) else "unknown"
            )
            port = values.get("port")
            found.append(
                _make_point(
                    source=address,
                    target=target,
                    attributes={
                        **base,
                        "via": "attachment",
                        "port": port,
                        "external": resolved is None,
                    },
                    hcl=attachment["address"],
                    port=str(port),
                    discriminator="attachment",
                )
            )
        return found

    # -- identity maps -----------------------------------------------------

    @staticmethod
    def _of_type(resources: list[dict], rtype: str) -> list[dict]:
        return [
            r
            for r in resources
            if r.get("type") == rtype and isinstance(r.get("values"), dict)
        ]

    @classmethod
    def _by_arn(cls, resources: list[dict], rtype: str) -> dict[str, dict]:
        """Map every ARN/id spelling of ``rtype`` to its resource."""
        mapping: dict[str, dict] = {}
        for resource in cls._of_type(resources, rtype):
            values = resource["values"]
            for key in ("arn", "id"):
                value = values.get(key)
                if isinstance(value, str) and value:
                    mapping.setdefault(value, resource)
        return mapping

    @classmethod
    def _task_definition_map(cls, resources: list[dict]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for resource in cls._of_type(resources, "aws_ecs_task_definition"):
            values = resource["values"]
            for key in ("arn", "arn_without_revision", "id"):
                value = values.get(key)
                if isinstance(value, str) and value:
                    mapping.setdefault(value, resource["address"])
        return mapping

    @classmethod
    def _registrable_targets(cls, resources: list[dict]) -> dict[str, str]:
        """What an attachment's ``target_id`` can name: instance, lambda, ALB."""
        mapping: dict[str, str] = {}
        for rtype, keys in (
            ("aws_instance", ("id", "arn")),
            ("aws_lambda_function", ("arn", "invoke_arn", "qualified_arn")),
            ("aws_lb", ("arn", "id")),
        ):
            for resource in cls._of_type(resources, rtype):
                values = resource["values"]
                for key in keys:
                    value = values.get(key)
                    if isinstance(value, str) and value:
                        mapping.setdefault(value, resource["address"])
        return mapping
