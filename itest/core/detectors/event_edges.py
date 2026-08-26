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
_REFERENCE_TYPES = {"aws_lambda_event_source_mapping", "aws_lambda_permission"}


def _point_id(source: str, target: str, mechanism: str) -> str:
    raw = "|".join(["event_edge", source, target, mechanism])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _make_point(
    source: str, target: str, attributes: dict, hcl: str
) -> IntegrationPoint:
    now = datetime.now(UTC)
    return IntegrationPoint(
        id=_point_id(source, target, attributes["mechanism"]),
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
    }

    def detect(self, plan_json: dict) -> list[IntegrationPoint]:
        from itest.core.detectors.base import iter_resources

        resources = [r for r in iter_resources(plan_json) if r.get("mode") == "managed"]
        lookup = self._identity_map(resources)

        def resolve(value) -> tuple[str, bool]:
            """Return (label, external)."""
            if not isinstance(value, str) or not value:
                return ("", True)
            address = lookup.get(value)
            return (address, False) if address else (value, True)

        points: list[IntegrationPoint] = []
        for resource in resources:
            rtype = resource.get("type")
            values = resource.get("values") or {}
            address = resource["address"]

            if rtype == "aws_lambda_event_source_mapping":
                source, src_ext = resolve(values.get("event_source_arn"))
                target, tgt_ext = resolve(
                    values.get("function_arn") or values.get("function_name")
                )
                if not source or not target:
                    continue
                points.append(
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
                points.append(
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
                target, tgt_ext = resolve(values.get("function_name"))
                if not source or not target:
                    continue
                points.append(
                    _make_point(
                        source,
                        target,
                        {
                            "mechanism": "lambda_permission",
                            "action": values.get("action"),
                            "principal": principal,
                            "external": src_ext or tgt_ext,
                        },
                        address,
                    )
                )
        return points

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
