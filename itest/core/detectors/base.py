"""Detector interface, shared plan-walking helpers, and the registry.

A detector consumes parsed ``terraform show -json`` output and emits typed
primitive integration points. Detectors are registered by appending to
:data:`DETECTORS`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from itest.core.manifest import IntegrationPoint


class Detector(ABC):
    """Base class for all detectors."""

    #: Terraform resource types this detector claims. Used by
    #: :func:`detect_all` to compute which resources went un-analyzed.
    handled_types: set[str] = set()

    @abstractmethod
    def detect(self, plan_json: dict) -> list[IntegrationPoint]:
        """Return the integration points found in ``plan_json``."""
        raise NotImplementedError


def _root_module(plan_json: dict) -> dict:
    """Return the root module, whether from a plan or a state show."""
    container = plan_json.get("planned_values") or plan_json.get("values") or {}
    return container.get("root_module", {}) or {}


def iter_resources(plan_json: dict) -> Iterator[dict]:
    """Yield every resource in the plan, descending into child modules."""

    def walk(module: dict) -> Iterator[dict]:
        yield from module.get("resources", []) or []
        for child in module.get("child_modules", []) or []:
            yield from walk(child)

    yield from walk(_root_module(plan_json))


# The active detector registry. Import-time population keeps wiring trivial.
from itest.core.detectors.event_edges import EventEdgeDetector  # noqa: E402
from itest.core.detectors.iam_edges import IamEdgeDetector  # noqa: E402
from itest.core.detectors.lb_edges import LoadBalancerEdgeDetector  # noqa: E402
from itest.core.detectors.route_edges import RouteEdgeDetector  # noqa: E402
from itest.core.detectors.sg_edges import SecurityGroupEdgeDetector  # noqa: E402

DETECTORS: list[Detector] = [
    SecurityGroupEdgeDetector(),
    IamEdgeDetector(),
    EventEdgeDetector(),
    RouteEdgeDetector(),
    LoadBalancerEdgeDetector(),
]


def detect_all(plan_json: dict) -> tuple[list[IntegrationPoint], dict[str, int]]:
    """Run every registered detector.

    Returns ``(points, unanalyzed_type_counts)`` where the second element maps
    each managed resource type that no detector handled to how many instances
    of it appear in the plan. This is what lets ``itest plan`` report
    "not analyzed" counts instead of silently skipping resources.
    """
    points: list[IntegrationPoint] = []
    handled: set[str] = set()
    for detector in DETECTORS:
        points.extend(detector.detect(plan_json))
        handled |= detector.handled_types

    unanalyzed: dict[str, int] = {}
    for resource in iter_resources(plan_json):
        if resource.get("mode") != "managed":
            continue
        rtype = resource.get("type")
        if not rtype or rtype in handled:
            continue
        unanalyzed[rtype] = unanalyzed.get(rtype, 0) + 1

    return points, unanalyzed
