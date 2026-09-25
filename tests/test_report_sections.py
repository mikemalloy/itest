"""The page shows what this project has, and advertises nothing.

Two rules, kept distinct:

- **Roadmap cards never render**, for any project. A release report is not
  the place to advertise a feature; the Database and Queue probe cards and
  their nav entries are gone, and their text lives in ``docs/report.md``.
- **Terraform-side sections hide for a declarations-only project.** The
  Infrastructure tiles, the API access sweep, the Integration graph and "Not
  analyzed" are real sections for a project that reads Terraform. For a
  project that declares tools and reads no Terraform they are four ways of
  saying "nothing here", so they are not rendered, and ONE footer line says
  so — which is what keeps "named, never silently skipped" honest. A project
  with Terraform renders every section exactly as before.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_reference_mcp_example import copy_example
from typer.testing import CliRunner

from itest.cli import app
from itest.core.manifest import IntegrationPoint, Manifest, load_manifest
from itest.report import model as report_model
from itest.report import render as report_render

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEX_S7 = REPO_ROOT / "tests" / "fixtures" / "alex" / "alex-s7.json"
TOOL_LEDGER = REPO_ROOT / "tests" / "fixtures" / "report" / "tool-ledger.json"
TEMPLATE = REPO_ROOT / "itest" / "report" / "templates" / "readiness.html"
DOCS = REPO_ROOT / "docs" / "report.md"
NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)

#: The roadmap cards' words. None of them may reach any page.
ROADMAP = ("Database probes", "Queue delivery", "Designed · not yet built")
#: The Terraform-side section headings.
TERRAFORM_SECTIONS = ("Infrastructure", "API access sweep", "Integration graph")
NOT_ANALYZED = "Not analyzed"
#: The section anchors, as the template spells them.
TERRAFORM_IDS = ('id="api"', 'id="graph"', 'id="notanalyzed"', 'id="postureGrid"')
ROADMAP_IDS = ('id="database"', 'id="queue"')
#: The one line the footer carries instead.
FOOTER = "No Terraform in this project; infrastructure sections are not shown."


@pytest.fixture(scope="module")
def reference_mcp(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, Manifest]:
    """reference-mcp over stdio, synced and verified: a declarations-only
    project, exactly as a user runs it."""
    example = copy_example(tmp_path_factory.mktemp("sections") / "reference-mcp")
    patch = pytest.MonkeyPatch()
    patch.chdir(example)
    patch.setenv("REFERENCE_MCP_TOKEN", "dry-run-token")
    for name in (
        "REFERENCE_MCP_OPEN_URL",
        "ITEST_PROMPTFOO_RESULTS",
        "ITEST_ENVIRONMENT",
    ):
        patch.delenv(name, raising=False)
    try:
        result = runner.invoke(app, ["sync", "--auto-approve", "--allow-unreachable"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["verify", "--output", "json"])
        assert result.exit_code == 0, result.output
        verify = json.loads(result.output)
        manifest = load_manifest(example / ".itest" / "manifest.yaml")
    finally:
        patch.undo()
    return verify, manifest


@pytest.fixture
def alex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Manifest]:
    """A Terraform-backed project: alex stage 7, synced from its state JSON."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX_S7)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["verify", "--output", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output), load_manifest(
        tmp_path / ".itest" / "manifest.yaml"
    )


def _render(verify: dict, manifest: Manifest) -> tuple[report_model.Page, str, dict]:
    page = report_model.build(verify, manifest, generated_at=NOW)
    html = report_render.render(page)
    return page, html, report_render.extract_blocks(html)


def _footer(blocks: dict) -> list[str]:
    return [entry["v"] for entry in blocks["PAGE"]["footer"]]


# --- roadmap cards ------------------------------------------------------------------


def test_the_template_carries_no_roadmap_card() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for word in ROADMAP:
        assert word not in text, word
    for anchor in ROADMAP_IDS:
        assert anchor not in text, anchor


@pytest.mark.slow
def test_no_page_advertises_a_roadmap_card(reference_mcp, alex) -> None:
    for verify, manifest in (reference_mcp, alex):
        _page, html, blocks = _render(verify, manifest)
        for word in ROADMAP:
            assert word not in html, word
        for anchor in ROADMAP_IDS:
            assert anchor not in html, anchor
        ids = [entry["id"] for entry in blocks["PAGE"]["nav"]]
        assert "database" not in ids and "queue" not in ids


def test_the_roadmap_text_moved_to_the_docs() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "## Roadmap" in text
    assert "Database" in text and "Queue" in text
    assert "Designed · not yet built" not in text


# --- a declarations-only project ---------------------------------------------------


@pytest.mark.slow
def test_the_reference_mcp_page_hides_the_terraform_sections(reference_mcp) -> None:
    verify, manifest = reference_mcp
    page, html, blocks = _render(verify, manifest)
    assert page.declarations_only is True
    for heading in (*TERRAFORM_SECTIONS, NOT_ANALYZED):
        assert heading not in html, heading
    for anchor in TERRAFORM_IDS:
        assert anchor not in html, anchor
    assert [entry["id"] for entry in blocks["PAGE"]["nav"]] == ["posture", "tools"]
    # The sections' words are not emitted for a page that does not draw them.
    assert blocks["SWEEP"] == [] and blocks["POINTS"] == [] and blocks["CHAIN"] == []
    for key in ("api", "graph", "notAnalyzed"):
        assert key not in blocks["PAGE"], key


@pytest.mark.slow
def test_the_reference_mcp_footer_says_so_once(reference_mcp) -> None:
    verify, manifest = reference_mcp
    _page, html, blocks = _render(verify, manifest)
    assert _footer(blocks).count(FOOTER) == 1
    assert html.count(FOOTER) == 1


@pytest.mark.slow
def test_the_answer_block_is_untouched_by_the_rule(reference_mcp) -> None:
    """The rule lives below the divider: the Answer's data is the same with
    the sections hidden as it would be with them drawn."""
    verify, manifest = reference_mcp
    page = report_model.build(verify, manifest, generated_at=NOW)
    assert page.answer == report_model.build(verify, manifest, generated_at=NOW).answer
    assert page.declarations_only is True
    assert page.answer.word and page.answer.sentence


# --- a Terraform project -------------------------------------------------------------


def test_the_terraform_page_keeps_every_section(alex) -> None:
    verify, manifest = alex
    page, html, blocks = _render(verify, manifest)
    assert page.declarations_only is False
    for heading in (*TERRAFORM_SECTIONS, NOT_ANALYZED):
        assert heading in html, heading
    for anchor in TERRAFORM_IDS:
        assert anchor in html, anchor
    assert [entry["id"] for entry in blocks["PAGE"]["nav"]] == [
        "posture",
        "tools",
        "api",
        "graph",
        "notanalyzed",
    ]
    assert blocks["POINTS"] and blocks["CHAIN"] and blocks["SWEEP"]
    for key in ("api", "graph", "notAnalyzed"):
        assert key in blocks["PAGE"], key
    assert FOOTER not in html


# --- how "declarations-only" is decided ---------------------------------------------


def _ledger() -> dict:
    return json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]


def _point(kind: str, ident: str) -> IntegrationPoint:
    return IntegrationPoint(
        id=ident,
        type=kind,
        source="a",
        target="b",
        hcl_address="x",
        first_seen=NOW,
        last_seen=NOW,
    )


def test_declarations_only_is_derived_from_the_absence_of_terraform_data() -> None:
    """Nothing records whether a run read Terraform, so the page derives it
    conservatively: no infrastructure point, no route, no graph edge, and no
    not-analyzed census. Tool points alone do not count as Terraform."""
    tools_only = Manifest(
        generated_at=NOW,
        points=[_point("mcp_tool", "3f9a1c2b7d10"), _point("mcp_tool", "8c4e0b2f6a13")],
    )
    verify = {
        "points": [
            {"id": "3f9a1c2b7d10", "status": "passing", "source": "a", "target": "b"},
            {"id": "8c4e0b2f6a13", "status": "passing", "source": "a", "target": "b"},
        ],
        "tools": _ledger(),
    }
    page = report_model.build(verify, tools_only, generated_at=NOW)
    assert page.declarations_only is True

    # One security-group edge is Terraform, whatever else the page holds.
    with_terraform = tools_only.model_copy(deep=True)
    with_terraform.points.append(_point("sg_edge", "aaaaaaaaaaaa"))
    verify["points"].append(
        {"id": "aaaaaaaaaaaa", "status": "stub", "source": "a", "target": "b"}
    )
    page = report_model.build(verify, with_terraform, generated_at=NOW)
    assert page.declarations_only is False


def test_an_empty_run_with_no_tools_is_not_declarations_only() -> None:
    """A run with nothing at all is not a declarations-only project: it may be
    a Terraform project whose state was empty, and hiding the sections would
    hide the empty-but-named states that say so."""
    page = report_model.build(
        {"points": []}, Manifest(generated_at=NOW), generated_at=NOW
    )
    assert page.declarations_only is False


def test_a_not_analyzed_census_means_terraform_was_read() -> None:
    manifest = Manifest(generated_at=NOW, points=[_point("mcp_tool", "3f9a1c2b7d10")])
    verify = {
        "points": [
            {"id": "3f9a1c2b7d10", "status": "passing", "source": "a", "target": "b"}
        ],
        "tools": _ledger(),
    }
    page = report_model.build(verify, manifest, generated_at=NOW)
    assert page.declarations_only is True
    page.not_analyzed = {"aws_kms_key": 1}
    assert report_model.is_declarations_only(page) is False


def test_the_docs_state_the_rule() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert FOOTER in text
    assert "declarations-only" in text
