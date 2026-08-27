"""Regression tests: the plan-JSON entry point must accept both roots.

``terraform show -json`` with no plan file emits *state* JSON (top-level key
``values``), while ``terraform show -json tfplan`` emits *plan* JSON
(``planned_values``). Real users run both constantly; either must work, and a
document with neither must fail loudly naming both keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from itest.core.detectors.base import detect_all
from itest.core.planner import PlanInputError, load_plan_json

ALEX = Path(__file__).resolve().parent / "fixtures" / "alex"


def test_state_root_alex_s5_finds_aurora_edge() -> None:
    plan_json = json.loads((ALEX / "alex-s5.json").read_text(encoding="utf-8"))
    assert "values" in plan_json and "planned_values" not in plan_json

    points, _ = detect_all(plan_json)
    aurora = [
        p
        for p in points
        if p.type == "sg_edge"
        and p.source == "172.31.0.0/16"
        and p.target == "aws_security_group.aurora"
    ]
    assert len(aurora) == 1
    assert aurora[0].attributes["protocol"] == "tcp"
    assert aurora[0].attributes["ports"] == "5432"


def test_load_plan_json_accepts_state_root(tmp_path: Path) -> None:
    doc = {"format_version": "1.0", "values": {"root_module": {"resources": []}}}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert load_plan_json(path, tmp_path) == doc


def test_load_plan_json_accepts_plan_root(tmp_path: Path) -> None:
    doc = {"format_version": "1.0", "planned_values": {"root_module": {}}}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert load_plan_json(path, tmp_path) == doc


def test_load_plan_json_rejects_document_with_neither_root(tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    # A `terraform plan -json` stream line, a common wrong file to hand over.
    path.write_text(
        json.dumps({"@level": "info", "type": "planned_change"}), encoding="utf-8"
    )
    with pytest.raises(PlanInputError) as excinfo:
        load_plan_json(path, tmp_path)
    message = str(excinfo.value)
    assert "planned_values" in message
    assert "values" in message


def test_empty_state_is_named_as_such(tmp_path: Path) -> None:
    # `terraform show -json` in an initialized-but-never-applied directory
    # emits only the format version. Found on three directories during the
    # first estate sweep; the generic "neither plan nor state" message sent
    # the user looking at file formats instead of at the backend.
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"format_version": "1.0"}), encoding="utf-8")
    with pytest.raises(PlanInputError) as excinfo:
        load_plan_json(path, tmp_path)
    message = str(excinfo.value)
    assert "empty" in message.lower()
    assert "applied" in message.lower()
    assert "backend" in message.lower()
