"""Detector interface, shared plan-walking helpers, and the registry.

A detector consumes parsed ``terraform show -json`` output and emits typed
primitive integration points. ITest ships a single detector today
(security-group edges); more are registered by appending to :data:`DETECTORS`.
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


class PlanRootError(ValueError):
    """Raised when a document is neither a plan nor a state show."""


#: The two roots `terraform show -json` can emit: a plan file renders
#: ``planned_values``, and no plan file renders the current state as ``values``.
PLAN_ROOTS = ("planned_values", "values")


def _root_module(plan_json: dict) -> dict:
    """Return the root module, whether from a plan or a state show.

    Absent either root the document is not terraform JSON at all, which is a
    usage error worth naming: detecting nothing looks identical to a project
    that genuinely has no integration points.
    """
    for root in PLAN_ROOTS:
        container = plan_json.get(root)
        if container is not None:
            return container.get("root_module", {}) or {}

    raise PlanRootError(
        "Input has neither a `planned_values` root (a saved plan) nor a "
        "`values` root (current state), so there is nothing to analyze. "
        "Supply the output of `terraform show -json`, optionally against a "
        f"plan file. Found top-level keys: {sorted(plan_json) or 'none'}."
    )


def iter_resources(plan_json: dict) -> Iterator[dict]:
    """Yield every resource in the plan, descending into child modules."""

    def walk(module: dict) -> Iterator[dict]:
        yield from module.get("resources", []) or []
        for child in module.get("child_modules", []) or []:
            yield from walk(child)

    yield from walk(_root_module(plan_json))


# The active detector registry. Import-time population keeps wiring trivial.
from itest.core.detectors.sg_edges import SecurityGroupEdgeDetector  # noqa: E402

DETECTORS: list[Detector] = [SecurityGroupEdgeDetector()]


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
