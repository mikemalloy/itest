"""Type-aware presentation helpers for integration points.

Every place ITest prints a point — the plan changeset, the Mermaid diagram,
the generated stub name and docstring — needs a short summary of what the
edge *is*. Keeping those summaries here, keyed by point type, means adding a
detector never leaves another command printing ``None:None``.
"""

from __future__ import annotations

import re

from itest.core.manifest import IntegrationPoint


def _short(label: str) -> str:
    """Short node name: HCL addresses drop the type prefix; ARNs keep the tail."""
    if label == "0.0.0.0/0":
        return "internet"
    if label.startswith("arn:"):
        # arn:partition:service:region:account:resource — keep service and
        # resource, drop the noise in between; wildcards read as "any".
        parts = label.split(":", 5)
        service = parts[2] if len(parts) > 2 else "arn"
        resource = parts[5] if len(parts) > 5 else ""
        resource = resource.replace("*", "any")
        return f"{service}_{resource}" if resource else service
    if label.startswith("aws_") and "." in label:
        return label.split(".", 1)[1]
    return label


def slug(label: str) -> str:
    """Identifier-safe fragment for use in generated function names."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", _short(label)).strip("_") or "x"


def _action_summary(actions: list[str]) -> str:
    """``rds-data:*x5`` style summary: service prefix plus action count."""
    if not actions:
        return "?"
    services = sorted({a.split(":", 1)[0] for a in actions})
    if len(actions) == 1:
        return actions[0]
    return f"{','.join(services)} ({len(actions)} actions)"


def summary(point: IntegrationPoint) -> str:
    """One-line tag describing the edge, e.g. ``tcp:5432 ingress``."""
    attrs = point.attributes
    if point.type == "sg_edge":
        return f"{attrs.get('protocol')}:{attrs.get('ports')} {attrs.get('direction')}"
    if point.type == "iam_edge":
        if attrs.get("managed"):
            tag = "managed policy"
            if attrs.get("broad_managed_policy"):
                tag += " BROAD"
            return tag
        tag = _action_summary(list(attrs.get("actions") or []))
        flags = [
            name
            for name in ("wildcard_action", "wildcard_resource", "external")
            if attrs.get(name)
        ]
        if attrs.get("effect") == "Deny":
            flags.insert(0, "DENY")
        return tag + (f" [{', '.join(flags)}]" if flags else "")
    if point.type == "event_edge":
        mechanism = attrs.get("mechanism", "")
        extra = ""
        if mechanism == "dlq_redrive":
            extra = f" max_receive={attrs.get('max_receive_count')}"
        elif mechanism == "event_source_mapping":
            extra = f" batch={attrs.get('batch_size')}"
        elif mechanism == "s3_notification":
            extra = " " + ",".join(attrs.get("events") or [])
            suffix = attrs.get("filter_suffix")
            prefix = attrs.get("filter_prefix")
            if suffix:
                extra += f" [suffix {suffix}]"
            elif prefix:
                extra += f" [prefix {prefix}]"
        elif mechanism == "lambda_permission":
            extra = f" {attrs.get('action')}"
            if attrs.get("qualifier"):
                extra += f" @{attrs['qualifier']}"
        return mechanism + extra
    return point.type


def diagram_label(point: IntegrationPoint) -> str:
    """Edge label for the Mermaid diagram — terse, one token where possible."""
    attrs = point.attributes
    if point.type == "sg_edge":
        return f"{attrs.get('protocol')}:{attrs.get('ports')}"
    if point.type == "iam_edge":
        if attrs.get("managed"):
            return "managed"
        actions = list(attrs.get("actions") or [])
        services = sorted({a.split(":", 1)[0] for a in actions})
        label = "/".join(services) if len(actions) != 1 else actions[0]
        if attrs.get("effect") == "Deny":
            label = "DENY " + label
        return label
    if point.type == "event_edge":
        return str(attrs.get("mechanism", "event"))
    return point.type


def function_name(point: IntegrationPoint) -> str:
    """Readable pytest function name, e.g. ``test_sg_web_to_db_5432``."""
    source = slug(point.source)
    target = slug(point.target)
    attrs = point.attributes
    if point.type == "sg_edge":
        ports = str(attrs.get("ports", "")).replace("-", "_")
        return f"test_sg_{source}_to_{target}_{ports}"
    if point.type == "iam_edge":
        return f"test_iam_{source}_to_{target}"
    if point.type == "event_edge":
        mechanism = slug(str(attrs.get("mechanism", "event")))
        return f"test_event_{source}_to_{target}_{mechanism}"
    return f"test_{point.type}_{source}_to_{target}"
