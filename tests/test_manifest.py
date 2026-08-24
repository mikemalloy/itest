from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from itest.core.manifest import (
    IntegrationPoint,
    Manifest,
    TestEntry,
    load_manifest,
    save_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "docs" / "manifest-example.yaml"


def _sample_manifest() -> Manifest:
    ts = datetime(2026, 8, 24, 17, 30, tzinfo=UTC)
    point = IntegrationPoint(
        id="a1b2c3d4",
        type="sg_edge",
        source="0.0.0.0/0",
        target="aws_security_group.alb",
        attributes={"protocol": "tcp", "ports": "443", "direction": "ingress"},
        hcl_address="aws_security_group.alb.ingress[0]",
        origin="detected",
        first_seen=ts,
        last_seen=ts,
    )
    other = IntegrationPoint(
        id="e5f6a7b8",
        type="sg_edge",
        source="aws_security_group.web",
        target="aws_security_group.db",
        attributes={"protocol": "tcp", "ports": "5432", "direction": "ingress"},
        hcl_address="aws_security_group_rule.db_from_web",
        origin="detected",
        first_seen=ts,
        last_seen=ts,
    )
    test = TestEntry(
        id="t-0001",
        point_id="a1b2c3d4",
        path="itest_tests/test_sg_edges.py",
        test_name="test_sg_internet_to_alb_443",
        ownership_hash="deadbeef" * 8,
        status="implemented",
        labels=["smoke"],
    )
    return Manifest(generated_at=ts, points=[point, other], tests=[test])


def test_round_trip(tmp_path: Path) -> None:
    original = _sample_manifest()
    path = tmp_path / "manifest.yaml"
    save_manifest(original, path)
    loaded = load_manifest(path)
    assert loaded == original


def test_canonical_address() -> None:
    entry = TestEntry(
        id="t-1",
        point_id="p-1",
        path="itest_tests/test_sg_edges.py",
        test_name="test_x",
        ownership_hash="0" * 64,
    )
    assert entry.canonical == "itest_tests/test_sg_edges.py::test_x"


def test_coverage_summary() -> None:
    m = _sample_manifest()
    # Point a1b2c3d4 has one implemented, enabled test -> covered.
    # Point e5f6a7b8 has no tests -> uncovered.
    summary = m.coverage_summary()
    assert summary.total_points == 2
    assert summary.covered == 1
    assert summary.uncovered == 1

    # A disabled test does not count as coverage.
    m.tests[0].disabled = True
    assert m.coverage_summary().covered == 0

    # Neither does an orphaned one.
    m.tests[0].disabled = False
    m.tests[0].status = "orphaned"
    assert m.coverage_summary().covered == 0


def test_get_point_and_tests_for_point() -> None:
    m = _sample_manifest()
    assert m.get_point("a1b2c3d4") is not None
    assert m.get_point("missing") is None
    assert len(m.tests_for_point("a1b2c3d4")) == 1
    assert m.tests_for_point("e5f6a7b8") == []


def test_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "future.yaml"
    path.write_text(
        "schema_version: 2\n"
        'generated_at: "2026-08-24T17:30:00+00:00"\n'
        "points: []\n"
        "tests: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema_version"):
        load_manifest(path)


def test_example_doc_validates() -> None:
    manifest = load_manifest(EXAMPLE)
    assert manifest.schema_version == 1
    assert len(manifest.points) == 2
    assert len(manifest.tests) == 3
    # Exercise the documented optional/edge fields.
    disabled = [t for t in manifest.tests if t.disabled]
    assert len(disabled) == 1
    assert disabled[0].disabled_reason
