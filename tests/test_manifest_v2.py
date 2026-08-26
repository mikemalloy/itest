"""Manifest schema v2: tier, resource_group, last_duration_seconds.

Schema only — no runner changes. v1 manifests must load transparently with
defaults filled and save back as v2; anything newer than v2 still errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from itest.cli import app
from itest.core.manifest import SCHEMA_VERSION, load_manifest, save_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
STUB_FILE = "itest_tests/test_sg_edges.py"
SKIP_LINE = 'pytest.skip("stub: implement this integration test")'

runner = CliRunner()

V1_DOC = """\
schema_version: 1
generated_at: "2026-08-24T17:30:00+00:00"
points:
  - id: "a1b2c3d4"
    type: "sg_edge"
    source: "0.0.0.0/0"
    target: "aws_security_group.alb"
    attributes: {protocol: tcp, ports: "443", direction: ingress}
    hcl_address: "aws_security_group.alb.ingress[0]"
    origin: "detected"
    first_seen: "2026-08-20T09:00:00+00:00"
    last_seen: "2026-08-24T17:30:00+00:00"
tests:
  - id: "t-0001"
    point_id: "a1b2c3d4"
    path: "itest_tests/test_sg_edges.py"
    test_name: "test_sg_internet_to_alb_443"
    ownership_hash: "deadbeef"
    status: "stub"
    disabled: false
    disabled_reason: null
    labels: []
"""


def test_schema_version_is_two() -> None:
    assert SCHEMA_VERSION == 2


def test_v1_manifest_migrates_transparently(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(V1_DOC, encoding="utf-8")

    manifest = load_manifest(path)
    assert manifest.schema_version == 2
    entry = manifest.tests[0]
    assert entry.tier == "readonly"
    assert entry.last_duration_seconds is None
    # Migration fills resource_group from the point's target identity.
    assert entry.resource_group == "aws_security_group.alb"


def test_v1_to_v2_round_trip_persists_new_fields(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(V1_DOC, encoding="utf-8")
    manifest = load_manifest(path)
    manifest.tests[0].tier = "active"
    manifest.tests[0].last_duration_seconds = 1.25
    manifest.tests[0].resource_group = "db"
    save_manifest(manifest, path)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    entry = raw["tests"][0]
    assert entry["tier"] == "active"
    assert entry["last_duration_seconds"] == 1.25
    assert entry["resource_group"] == "db"

    again = load_manifest(path)
    assert again.tests[0].tier == "active"
    assert again.tests[0].last_duration_seconds == 1.25
    assert again.tests[0].resource_group == "db"


def test_tier_is_validated(tmp_path: Path) -> None:
    doc = V1_DOC.replace('status: "stub"', 'status: "stub"\n    tier: "turbo"')
    path = tmp_path / "manifest.yaml"
    path.write_text(doc, encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(path)


def test_v3_manifest_still_errors(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(V1_DOC.replace("schema_version: 1", "schema_version: 3"))
    with pytest.raises(ValueError, match="schema_version"):
        load_manifest(path)


@pytest.fixture
def synced_project(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_sync_populates_resource_group_and_writes_v2(synced_project: Path) -> None:
    manifest_path = synced_project / ".itest" / "manifest.yaml"
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2

    manifest = load_manifest(manifest_path)
    assert manifest.tests
    for entry in manifest.tests:
        point = manifest.get_point(entry.point_id)
        assert point is not None
        assert entry.resource_group == point.target
        assert entry.tier == "readonly"
        assert entry.last_duration_seconds is None


def test_verify_records_last_duration(synced_project: Path) -> None:
    # Implement one stub so at least one test actually executes its body.
    path = synced_project / STUB_FILE
    text = path.read_text()
    i = text.index("def test_sg_web_to_db_5432():")
    j = text.index(SKIP_LINE, i)
    end = text.index("\n", j) + 1
    path.write_text(text[:j] + "assert 1 + 1 == 2" + "\n" + text[end:])

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output

    manifest = load_manifest(synced_project / ".itest" / "manifest.yaml")
    by_name = {t.test_name: t for t in manifest.tests}
    implemented = by_name["test_sg_web_to_db_5432"]
    assert implemented.last_duration_seconds is not None
    assert implemented.last_duration_seconds >= 0.0
    # Skipped stubs still ran through pytest and get a duration too.
    for entry in manifest.tests:
        assert entry.last_duration_seconds is not None
