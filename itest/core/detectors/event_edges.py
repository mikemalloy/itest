"""Event / invocation edge detector.

Emits one ``event_edge`` integration point per piece of asynchronous wiring
that makes one resource *drive* another:

- ``aws_lambda_event_source_mapping`` — event source (queue, stream) ->
  function. ``mechanism: event_source_mapping`` with ``batch_size`` and
  ``enabled``.
- SQS redrive — an ``aws_sqs_queue`` whose ``redrive_policy`` names a
  ``deadLetterTargetArn`` emits queue -> DLQ. ``mechanism: dlq_redrive`` with
  ``max_receive_count``.
- ``aws_lambda_permission`` — principal (or its ``source_arn`` when given)
  -> function. ``mechanism: lambda_permission`` with ``action`` and
  ``principal``.

Endpoints given as ARNs or names resolve to HCL addresses when the referenced
resource is present in the same plan/state; otherwise the raw value is kept
and the point is flagged ``external: true``. Point IDs hash the edge content
(type, source, target, mechanism), never array positions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from itest.core.manifest import IntegrationPoint

#: Resource types that only *point at* other resources and carry no identity
#: of their own worth resolving to.
_REFERENCE_TYPES = {
    "aws_lambda_event_source_mapping",
    "aws_lambda_permission",
    # A notification carries its bucket's name as its own id; letting that
    # into the identity map would shadow the bucket it points at.
    "aws_s3_bucket_notification",
    # A target is a pure reference: its id and arn name the rule
    # and destination it binds, not anything of its own.
    "aws_cloudwatch_event_target",
}


def _point_id(
    source: str,
    target: str,
    mechanism: str,
    qualifier: str | None = None,
    discriminator: str | None = None,
) -> str:
    parts = ["event_edge", source, target, mechanism]
    # A Lambda permission on a version/alias is a different grant from the
    # unqualified one (terraform-aws-modules/lambda creates both per trigger).
    # Appended only when non-empty so existing ids are unchanged.
    if qualifier:
        parts.append(f"qualifier={qualifier}")
    # Two notifications from one bucket to one queue differing only by filter
    # are two integrations. Appended only when given, so ids predating this
    # are unchanged.
    if discriminator:
        parts.append(discriminator)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _rule_enabled(values: dict) -> bool:
    """Whether a rule is live, across both provider spellings.

    Newer providers record ``state`` ("ENABLED"/"DISABLED"); older ones record
    a boolean ``is_enabled``. Reading only the new one would report every rule
    from an older state as disabled.
    """
    state = values.get("state")
    if isinstance(state, str) and state:
        return state.upper() == "ENABLED"
    return bool(values.get("is_enabled", True))


def _make_point(
    source: str,
    target: str,
    attributes: dict,
    hcl: str,
    discriminator: str | None = None,
) -> IntegrationPoint:
    now = datetime.now(UTC)
    return IntegrationPoint(
        id=_point_id(
            source,
            target,
            attributes["mechanism"],
            attributes.get("qualifier"),
            discriminator,
        ),
        type="event_edge",
        source=source,
        target=target,
        attributes=attributes,
        hcl_address=hcl,
        origin="detected",
        first_seen=now,
        last_seen=now,
    )


class EventEdgeDetector:
    """Detect event-driven invocation edges."""

    handled_types = {
        "aws_lambda_event_source_mapping",
        "aws_lambda_permission",
        "aws_sqs_queue",
        "aws_s3_bucket_notification",
        "aws_cloudwatch_event_rule",
        "aws_cloudwatch_event_target",
    }

    def detect(self, plan_json: dict) -> list[IntegrationPoint]:
        from itest.core.detectors.base import iter_resources

        resources = [r for r in iter_resources(plan_json) if r.get("mode") == "managed"]
        lookup = self._identity_map(resources)
        functions = self._function_map(resources)
        rules = self._rule_map(resources)

        def resolve(value) -> tuple[str, bool]:
            """Return (label, external)."""
            if not isinstance(value, str) or not value:
                return ("", True)
            address = lookup.get(value)
            return (address, False) if address else (value, True)

        def resolve_function(value) -> tuple[str, bool]:
            """Resolve a Lambda function reference against functions FIRST.

            A ``function_name`` names a Lambda; resolving it against every
            resource lets a same-named IAM role (terraform-aws-modules/lambda
            names both identically) win by sort order.
            """
            if isinstance(value, str) and value in functions:
                return (functions[value], False)
            return resolve(value)

        def resolve_bucket(value) -> tuple[str, bool]:
            """A notification names its bucket bare; try the name then its ARN."""
            if not isinstance(value, str) or not value:
                return ("", True)
            address = lookup.get(value) or lookup.get(f"arn:aws:s3:::{value}")
            return (address, False) if address else (value, True)

        # Pre-scan lambda_permission grants for base-key collisions (F10): two
        # grants sharing (source, target, qualifier) but differing by action
        # (InvokeFunction vs InvokeFunctionUrl, no source_arn) would share one id
        # and collapse. Only those get an action discriminator below, so every
        # non-colliding permission id is unchanged.
        perm_actions: dict[tuple[str, str, str], set[str]] = {}
        for resource in resources:
            if resource.get("type") != "aws_lambda_permission":
                continue
            values = resource.get("values") or {}
            source_arn = values.get("source_arn")
            source = (
                resolve(source_arn)[0]
                if source_arn
                else (values.get("principal") or "")
            )
            target = resolve_function(values.get("function_name"))[0]
            if not source or not target:
                continue
            key = (source, target, values.get("qualifier") or "")
            perm_actions.setdefault(key, set()).add(values.get("action") or "")
        colliding_perm_keys = {
            k for k, actions in perm_actions.items() if len(actions) > 1
        }

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

            if rtype == "aws_lambda_event_source_mapping":
                source, src_ext = resolve(values.get("event_source_arn"))
                target, tgt_ext = resolve_function(
                    values.get("function_arn") or values.get("function_name")
                )
                if not source or not target:
                    continue
                emit(
                    _make_point(
                        source,
                        target,
                        {
                            "mechanism": "event_source_mapping",
                            "batch_size": values.get("batch_size"),
                            "enabled": values.get("enabled"),
                            "external": src_ext or tgt_ext,
                        },
                        address,
                    )
                )

            elif rtype == "aws_sqs_queue":
                policy = self._redrive(values.get("redrive_policy"))
                if not policy:
                    continue
                target, tgt_ext = resolve(policy.get("deadLetterTargetArn"))
                if not target:
                    continue
                emit(
                    _make_point(
                        address,
                        target,
                        {
                            "mechanism": "dlq_redrive",
                            "max_receive_count": policy.get("maxReceiveCount"),
                            "external": tgt_ext,
                        },
                        address,
                    )
                )

            elif rtype == "aws_lambda_permission":
                principal = values.get("principal") or ""
                source_arn = values.get("source_arn")
                if source_arn:
                    source, src_ext = resolve(source_arn)
                else:
                    source, src_ext = principal, True
                target, tgt_ext = resolve_function(values.get("function_name"))
                if not source or not target:
                    continue
                key = (source, target, values.get("qualifier") or "")
                # Append-only: the action rides the id only when a same-key
                # sibling with a different action actually exists.
                discriminator = (
                    f"action={values.get('action') or ''}"
                    if key in colliding_perm_keys
                    else None
                )
                emit(
                    _make_point(
                        source,
                        target,
                        {
                            "mechanism": "lambda_permission",
                            "action": values.get("action"),
                            "principal": principal,
                            "qualifier": values.get("qualifier") or "",
                            "external": src_ext or tgt_ext,
                        },
                        address,
                        discriminator=discriminator,
                    )
                )
            elif rtype == "aws_s3_bucket_notification":
                bucket, bucket_ext = resolve_bucket(values.get("bucket"))
                if not bucket:
                    continue
                for destination, arn_key in (
                    ("queue", "queue_arn"),
                    ("topic", "topic_arn"),
                    ("lambda_function", "lambda_function_arn"),
                ):
                    for entry in values.get(destination) or []:
                        if not isinstance(entry, dict):
                            continue
                        target, tgt_ext = resolve(entry.get(arn_key))
                        if not target:
                            continue
                        events = sorted(str(e) for e in entry.get("events") or [])
                        prefix = entry.get("filter_prefix") or ""
                        suffix = entry.get("filter_suffix") or ""
                        emit(
                            _make_point(
                                source=bucket,
                                target=target,
                                attributes={
                                    "mechanism": "s3_notification",
                                    "events": events,
                                    "filter_prefix": prefix,
                                    "filter_suffix": suffix,
                                    "external": bucket_ext or tgt_ext,
                                },
                                hcl=address,
                                discriminator=f"{','.join(events)}|{prefix}|{suffix}",
                            )
                        )

                if values.get("eventbridge"):
                    # The default bus is not a Terraform resource anywhere, so
                    # it is always external: nothing in state can resolve it.
                    emit(
                        _make_point(
                            source=bucket,
                            target="eventbridge",
                            attributes={
                                "mechanism": "s3_notification",
                                "events": ["*"],
                                "filter_prefix": "",
                                "filter_suffix": "",
                                "external": True,
                            },
                            hcl=address,
                            discriminator="*||",
                        )
                    )

            elif rtype == "aws_cloudwatch_event_target":
                bus = values.get("event_bus_name") or "default"
                rule = rules.get((values.get("rule"), bus))
                if rule is None:
                    # A rule name is scoped to its bus; without a match on both
                    # there is no rule here to source the edge from.
                    continue
                target, tgt_ext = resolve(values.get("arn"))
                if not target:
                    continue
                rule_values = rule["values"]
                emit(
                    _make_point(
                        source=rule["address"],
                        target=target,
                        attributes={
                            "mechanism": "eventbridge_target",
                            "event_bus_name": bus,
                            "trigger": (
                                "schedule"
                                if rule_values.get("schedule_expression")
                                else "pattern"
                            ),
                            "enabled": _rule_enabled(rule_values),
                            "external": tgt_ext,
                        },
                        hcl=address,
                        # Never the target_id: Terraform generates it, so it
                        # changes on re-apply while the wiring does not.
                        discriminator=f"bus={bus}",
                    )
                )

        return points

    @staticmethod
    def _rule_map(resources: list[dict]) -> dict[tuple[str, str], dict]:
        """Map (rule name or ARN, bus) to the rule resource.

        EventBridge rule names are unique per bus, not per account, so the bus
        is part of the key: the same name on another bus is another rule.
        """
        mapping: dict[tuple[str, str], dict] = {}
        for resource in resources:
            if resource.get("type") != "aws_cloudwatch_event_rule":
                continue
            values = resource.get("values") or {}
            bus = values.get("event_bus_name") or "default"
            entry = {"address": resource["address"], "values": values}
            for key in ("name", "id", "arn"):
                value = values.get(key)
                if isinstance(value, str) and value:
                    mapping.setdefault((value, bus), entry)
        return mapping

    @staticmethod
    def _identity_map(resources: list[dict]) -> dict[str, str]:
        """Map every ARN / id / name a wiring resource might cite to an address.

        Wiring resources (mappings, permissions) are references, not
        identities: their ``function_name`` / ``event_source_arn`` values
        name *other* resources, so they are excluded from the map. First
        writer wins among the rest.
        """
        mapping: dict[str, str] = {}
        for resource in resources:
            if resource.get("type") in _REFERENCE_TYPES:
                continue
            values = resource.get("values") or {}
            for key in ("arn", "id", "function_name", "name", "url"):
                value = values.get(key)
                if isinstance(value, str) and value and value not in mapping:
                    mapping[value] = resource["address"]
        return mapping

    @staticmethod
    def _function_map(resources: list[dict]) -> dict[str, str]:
        """Map Lambda function names, ARNs, and ids to their addresses."""
        mapping: dict[str, str] = {}
        for resource in resources:
            if resource.get("type") != "aws_lambda_function":
                continue
            values = resource.get("values") or {}
            for key in ("arn", "function_name", "id", "qualified_arn"):
                value = values.get(key)
                if isinstance(value, str) and value and value not in mapping:
                    mapping[value] = resource["address"]
        return mapping

    @staticmethod
    def _redrive(raw) -> dict | None:
        if not raw:
            return None
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
