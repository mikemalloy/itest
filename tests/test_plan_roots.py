"""Plan and state JSON both have to parse through one entry point.

`terraform show -json` with no plan file emits a *state* document, rooted at
"values" rather than "planned_values". Both roots reach the detectors through
`_root_module`, and a document with neither must say so rather than quietly
detecting nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from itest.core.detectors.base import PlanRootError, detect_all

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"


def _plan_document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _state_document() -> dict:
    """The same content, re-rooted the way `terraform show -json` emits state."""
    document = _plan_document()
    state = {k: v for k, v in document.items() if k != "planned_values"}
    state["values"] = document["planned_values"]
    return state


def test_plan_root_is_detected() -> None:
    points, _ = detect_all(_plan_document())
    assert len(points) == 3


def test_state_root_is_detected() -> None:
    """A state document must not silently detect nothing."""
    points, _ = detect_all(_state_document())
    assert len(points) == 3


def test_both_roots_yield_identical_points() -> None:
    """Point ids come from addresses and rule content, not the root key."""
    plan_points, plan_unanalyzed = detect_all(_plan_document())
    state_points, state_unanalyzed = detect_all(_state_document())

    assert {p.id for p in state_points} == {p.id for p in plan_points}
    assert state_unanalyzed == plan_unanalyzed


def test_missing_both_roots_names_both_keys() -> None:
    """Neither root is a usage error, not an empty result."""
    document = {"format_version": "1.0", "terraform_version": "1.9.0"}

    with pytest.raises(PlanRootError) as excinfo:
        detect_all(document)

    message = str(excinfo.value)
    assert "planned_values" in message
    assert "values" in message
    assert "terraform show -json" in message


def test_empty_root_module_is_not_an_error() -> None:
    """A real but empty state has a root; zero points is the honest answer."""
    points, unanalyzed = detect_all({"values": {"root_module": {}}})
    assert points == []
    assert unanalyzed == {}
