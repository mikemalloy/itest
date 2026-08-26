"""Render detected integration points as a Mermaid flowchart."""

from __future__ import annotations

from itest.core import points as point_labels
from itest.core.manifest import IntegrationPoint


def generate_mermaid(points: list[IntegrationPoint]) -> str:
    """Return a Mermaid ``flowchart`` of all detected points.

    Nodes are resources, CIDRs, or external ARNs; edges are labelled per point
    type (``protocol:ports`` for sg_edge, action summary for iam_edge, the
    mechanism for event_edge) and drawn from source to target. Node order
    follows first appearance so the output is deterministic across runs.
    """
    lines = ["flowchart LR"]
    node_ids: dict[str, str] = {}

    def node_id(label: str) -> str:
        if label not in node_ids:
            node_ids[label] = f"n{len(node_ids)}"
        return node_ids[label]

    # Assign ids in first-seen order across both endpoints.
    for point in points:
        node_id(point.source)
        node_id(point.target)

    for label, ident in node_ids.items():
        lines.append(f'    {ident}["{label}"]')

    for point in points:
        label = point_labels.diagram_label(point)
        lines.append(
            f"    {node_id(point.source)} -->|{label}| {node_id(point.target)}"
        )

    return "\n".join(lines) + "\n"
