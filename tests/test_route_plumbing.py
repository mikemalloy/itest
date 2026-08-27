"""A new point type must reach every place ITest prints or generates.

The failure this guards against is silent: a detector ships, and the plan
changeset, the diagram, the stub name, or the verify point line renders an
empty tag for it. Adding a type means adding it to `points.py`; everything
downstream — stub routing, sync, verify — is supposed to follow with no
change at all, and these tests are how "supposed to" is checked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core import points as point_labels
from itest.core import stubgen
from itest.core.manifest import IntegrationPoint, load_manifest
from itest.core.mermaid import generate_mermaid

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEX_S3 = REPO_ROOT / "tests" / "fixtures" / "alex" / "alex-s3.json"
ALEX_S7 = REPO_ROOT / "tests" / "fixtures" / "alex" / "alex-s7.json"

ROUTE_FILE = "itest_tests/test_route_edges.py"


def _point(**overrides) -> IntegrationPoint:
    now = datetime.now(UTC)
    attributes = {
        "method": "ANY",
        "path": "/api/{proxy+}",
        "integration_type": "AWS_PROXY",
        "auth": "NONE",
        "api_key_required": False,
        "stages": ["$default"],
        "external": False,
    }
    attributes.update(overrides.pop("attributes", {}))
    data = {
        "id": "0123456789ab",
        "type": "route_edge",
        "source": "aws_apigatewayv2_api.main",
        "target": "aws_lambda_function.api",
        "attributes": attributes,
        "hcl_address": "aws_apigatewayv2_route.api_any",
        "first_seen": now,
        "last_seen": now,
    }
    data.update(overrides)
    return IntegrationPoint(**data)


# --------------------------------------------------------------------------
# Manifest schema
# --------------------------------------------------------------------------


def test_manifest_accepts_the_new_type() -> None:
    assert _point().type == "route_edge"


def test_manifest_still_rejects_an_unknown_type() -> None:
    """The literal is a guard, not a formality."""
    with pytest.raises(ValueError):
        _point(type="teapot_edge")


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------


def test_summary_reads_as_the_route_it_is() -> None:
    assert point_labels.summary(_point()) == "ANY /api/{proxy+} -> AWS_PROXY [open]"


def test_summary_flags_an_external_target() -> None:
    point = _point(
        attributes={
            "auth": "AWS_IAM",
            "external": True,
            "integration_type": "HTTP_PROXY",
        }
    )
    assert point_labels.summary(point) == "ANY /api/{proxy+} -> HTTP_PROXY [external]"


def test_diagram_label_is_the_route() -> None:
    assert point_labels.diagram_label(_point()) == "ANY /api/{proxy+}"


def test_diagram_carries_route_edges() -> None:
    diagram = generate_mermaid([_point()])
    assert "|ANY /api/{proxy+}|" in diagram
    assert '["aws_apigatewayv2_api.main"]' in diagram


def test_function_name_names_api_method_path_and_target() -> None:
    assert (
        point_labels.function_name(_point()) == "test_route_main_ANY_api_proxy_to_api"
    )


def test_function_names_differ_per_method_on_one_path() -> None:
    first = point_labels.function_name(_point())
    second = point_labels.function_name(_point(attributes={"method": "OPTIONS"}))
    assert first != second


def test_stubs_route_to_their_own_file() -> None:
    assert stubgen.stub_file_for(_point()) == ROUTE_FILE


# --------------------------------------------------------------------------
# sync and verify need no change of their own — prove it
# --------------------------------------------------------------------------


@pytest.fixture
def synced(tmp_path, monkeypatch):
    def _sync(fixture: Path) -> Path:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["sync", "--auto-approve", "--tf-json", str(fixture)]
        )
        assert result.exit_code == 0, result.output
        return tmp_path

    return _sync


def test_sync_writes_two_route_stubs_for_alex_s7(synced) -> None:
    workdir = synced(ALEX_S7)
    text = (workdir / ROUTE_FILE).read_text(encoding="utf-8")
    assert text.count("\ndef test_") == 2

    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    routes = [p for p in manifest.points if p.type == "route_edge"]
    assert len(routes) == 2
    for point in routes:
        entry = next(t for t in manifest.tests if t.point_id == point.id)
        assert entry.path == ROUTE_FILE


def test_generated_route_file_is_importable(synced) -> None:
    workdir = synced(ALEX_S7)
    source = (workdir / ROUTE_FILE).read_text(encoding="utf-8")
    compile(source, ROUTE_FILE, "exec")


def test_stub_docstring_carries_the_route(synced) -> None:
    """The generic attribute line already covers a new type's attributes."""
    workdir = synced(ALEX_S3)
    text = (workdir / ROUTE_FILE).read_text(encoding="utf-8")
    assert "aws_api_gateway_rest_api.api -> aws_lambda_function.ingest" in text
    assert "type=route_edge" in text
    assert "method=POST" in text
    assert "path=/ingest" in text


def test_verify_reports_route_points_with_their_tags(synced) -> None:
    synced(ALEX_S7)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output

    # The regression this guards: a type points.py does not know renders an
    # empty tag slot.
    assert "(:)" not in result.output
    assert "12 integration points" in result.output
    assert "12 stubs" in result.output

    lines = [
        line
        for line in result.output.splitlines()
        if "aws_apigatewayv2_api.main -> aws_lambda_function.api" in line
    ]
    assert len(lines) == 2, result.output
    assert any("(ANY /api/{proxy+} -> AWS_PROXY [open])" in line for line in lines)
    assert any("(OPTIONS /api/{proxy+} -> AWS_PROXY [open])" in line for line in lines)
    assert all(line.strip().startswith("[STUB]") for line in lines)


def test_plan_shows_the_open_flag(tmp_path, monkeypatch) -> None:
    """The flag has to be visible where a reviewer first sees the point."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plan", "--tf-json", str(ALEX_S3)])
    assert result.exit_code == 0, result.output
    assert "POST /ingest -> AWS_PROXY [open]" in result.output
