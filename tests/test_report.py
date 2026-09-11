"""Tests for the readiness page: data model, renderer, and the CLI end to end.

The page is engine-rendered, so the tests here are mostly about *provenance*:
every number must come from a verify JSON field or the manifest, the template's
example data must not survive into output, and an absent source must render as
a named empty state rather than as plausible-looking sample data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from itest.cli import app
from itest.core.manifest import Manifest, load_manifest
from itest.report import model as report_model

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
ALEX_S6 = FIXTURES / "alex" / "alex-s6.json"
ALEX_S7 = FIXTURES / "alex" / "alex-s7.json"
TOOL_LEDGER = FIXTURES / "report" / "tool-ledger.json"

GENERATED_AT = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def synced(tmp_path: Path, monkeypatch, fixture: Path) -> Path:
    """Sync ``fixture`` into ``tmp_path`` and return the project dir."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(fixture)])
    assert result.exit_code == 0, result.output
    return tmp_path


def verify_json(project: Path) -> dict:
    """Run `itest verify --output json` in ``project`` and parse it."""
    result = runner.invoke(app, ["verify", "--output", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.fixture
def alex_s7(tmp_path, monkeypatch) -> tuple[dict, Manifest]:
    project = synced(tmp_path, monkeypatch, ALEX_S7)
    return verify_json(project), load_manifest(project / ".itest" / "manifest.yaml")


# --------------------------------------------------------------------------
# The tool-ledger contract with P31.
# --------------------------------------------------------------------------


def test_tool_ledger_fixture_loads_through_the_model() -> None:
    """The committed fixture IS the contract; drift must fail a test.

    The models forbid extra keys, so a renamed or added field in the fixture
    fails here rather than silently rendering as nothing.
    """
    document = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))
    ledger = report_model.ToolLedger.model_validate(document["tools"])

    assert [s.server for s in ledger.servers] == ["reference-mcp"]
    server = ledger.servers[0]
    assert server.summary.declared == 6
    assert server.summary.changed == 1
    assert [f.id for f in server.families] == [
        "authority",
        "blast",
        "containment",
        "change",
    ]
    assert [t.name for t in server.tools] == ["delete_record", "update_record"]
    assert server.tools[0].checks[0].trait == "authority.anonymous"
    assert server.tools[1].checks[1].change.previous == "Update fields on a record."
    assert server.exceptions[0].kind == "changed"


def test_tool_ledger_vocabularies_match_the_model() -> None:
    """The fixture's declared vocabularies are the model's accepted values."""
    document = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))
    assert set(document["status_vocabulary"]) == set(report_model.CHECK_STATUSES)
    assert set(document["mutation_vocabulary"]) == set(report_model.MUTATIONS)
    assert set(document["mutation_source_vocabulary"]) == set(
        report_model.MUTATION_SOURCES
    )


def test_tool_ledger_rejects_an_unknown_field() -> None:
    """A field added to the ledger without extending the model fails loudly."""
    document = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))
    document["tools"]["servers"][0]["surprise"] = 1
    with pytest.raises(ValidationError):
        report_model.ToolLedger.model_validate(document["tools"])


# --------------------------------------------------------------------------
# Verdict rules.
# --------------------------------------------------------------------------


def test_verdict_stub_only_run_is_at_risk(alex_s7) -> None:
    """A run with no verified coverage never carries a green stamp."""
    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    assert page.verdict.word == "AT RISK"
    assert page.verdict.integrations_verified == 0
    assert page.verdict.integrations_total == 12


def test_verdict_blocked_when_a_point_fails(alex_s7) -> None:
    verify, manifest = alex_s7
    verify["points"][0]["status"] = "failing"
    verify["failing"] = 1
    verify["stubs"] = 11
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    assert page.verdict.word == "BLOCKED"


def test_verdict_blocked_when_a_point_errors(alex_s7) -> None:
    verify, manifest = alex_s7
    verify["points"][0]["status"] = "error"
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    assert page.verdict.word == "BLOCKED"


def test_verdict_verified_when_every_point_passes(alex_s7) -> None:
    verify, manifest = alex_s7
    for point in verify["points"]:
        point["status"] = "passing"
    verify["passing"] = len(verify["points"])
    verify["stubs"] = 0
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    assert page.verdict.word == "VERIFIED"
    assert page.verdict.integrations_verified == page.verdict.integrations_total


def test_verdict_at_risk_when_an_implemented_test_is_stuck_at_stub(alex_s7) -> None:
    """The manifest says implemented, the point reports stub: not verified."""
    verify, manifest = alex_s7
    for point in verify["points"]:
        point["status"] = "passing"
    verify["passing"] = len(verify["points"])
    # One point drops back to stub while its manifest entry claims implemented.
    stuck = verify["points"][0]["id"]
    verify["points"][0]["status"] = "stub"
    for entry in manifest.tests:
        if entry.point_id == stuck:
            entry.status = "implemented"
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    assert page.verdict.word == "AT RISK"


def test_verdict_blocked_by_a_critical_tool_check(alex_s7) -> None:
    verify, manifest = alex_s7
    for point in verify["points"]:
        point["status"] = "passing"
    verify["passing"] = len(verify["points"])
    ledger = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    ledger["servers"][0]["summary"]["critical"] = 1
    verify["tools"] = ledger
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    assert page.verdict.word == "BLOCKED"


def test_verdict_at_risk_when_a_tool_change_awaits_review(alex_s7) -> None:
    verify, manifest = alex_s7
    for point in verify["points"]:
        point["status"] = "passing"
    verify["passing"] = len(verify["points"])
    verify["tools"] = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    assert page.verdict.word == "AT RISK"
    assert page.verdict.tool_changes_to_review == 1
    assert page.verdict.tools_verified == 5
    assert page.verdict.tools_total == 6


# --------------------------------------------------------------------------
# Everything on the page traces to verify JSON or the manifest.
# --------------------------------------------------------------------------


def test_posture_counts_come_from_point_attributes(alex_s7) -> None:
    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    expected_external = sum(
        1 for p in verify["points"] if p["attributes"].get("external")
    )
    expected_wildcard = sum(
        1 for p in verify["points"] if p["attributes"].get("wildcard_resource")
    )
    assert page.posture.cross_stack == expected_external
    assert page.posture.wildcard == expected_wildcard
    assert page.posture.broad_managed == 0
    # Both alex route_edges are auth NONE.
    assert page.posture.unauthenticated_routes == 2


def test_api_sweep_rows_come_from_route_edge_points(alex_s7) -> None:
    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    endpoints = [e for g in page.api.groups for e in g.endpoints]
    assert {e.method for e in endpoints} == {"ANY", "OPTIONS"}
    assert {e.path for e in endpoints} == {"/api/{proxy+}"}
    # Stub-only run: the status token is what verify recorded, nothing more.
    assert {e.status for e in endpoints} == {"STUB"}


def test_api_sweep_collapses_to_one_status_column(alex_s7) -> None:
    """Verify JSON does not record which probe was the anonymous one.

    Until it carries a probe kind, the sweep shows ONE status column rather
    than an Unauthenticated/Authenticated pair holding the same neutral value.
    """
    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    assert page.api.probe_kind_known is False
    for group in page.api.groups:
        for endpoint in group.endpoints:
            assert endpoint.unauth is None
            assert endpoint.auth is None
    assert page.verdict.endpoints_refuse_anon is None
    assert page.verdict.authenticated_200 is None
    # What IS derivable: how many route_edge points verify verified.
    assert page.verdict.endpoints_verified == 0
    assert page.verdict.endpoints_total == 2


def test_graph_points_and_chain_come_from_the_manifest(alex_s7) -> None:
    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    iam_ids = {p.id for p in manifest.points if p.type == "iam_edge"}
    event_ids = {p.id for p in manifest.points if p.type == "event_edge"}
    assert {p.id for p in page.graph.points} == iam_ids
    assert {c.id for c in page.graph.chain} == event_ids
    external = next(p for p in page.graph.points if p.ext)
    assert external.id in iam_ids


def test_footer_reads_account_and_region_from_arns(alex_s7) -> None:
    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    assert page.footer.account == "111111111111"
    assert page.footer.region == "us-west-1"
    assert page.footer.elapsed_s == verify["elapsed_seconds"]
    assert page.footer.generated_at == "2026-09-10 12:00 UTC"


def test_undderivable_sources_are_none_not_invented(alex_s7) -> None:
    """Absent sources stay None; the renderer turns them into empty states."""
    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    assert page.tools is None
    assert page.not_analyzed is None
    assert page.footer.commit is None
    assert page.since is None


# --------------------------------------------------------------------------
# --since: the only thing that turns trends on.
# --------------------------------------------------------------------------


def test_since_is_off_by_default(alex_s7) -> None:
    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)

    assert page.since is None
    assert page.new_points == []
    assert page.removed_points == []
    for tile in page.posture.tiles():
        assert tile.trend is None


def test_since_marks_new_points_and_posture_trends(tmp_path, monkeypatch) -> None:
    """A prior manifest turns on new/removed marks and the posture deltas."""
    project = synced(tmp_path, monkeypatch, ALEX_S7)
    verify = verify_json(project)
    manifest = load_manifest(project / ".itest" / "manifest.yaml")

    # A prior release that had one fewer IAM grant.
    prior = manifest.model_copy(deep=True)
    dropped = next(p for p in prior.points if p.type == "iam_edge")
    prior.points = [p for p in prior.points if p.id != dropped.id]

    page = report_model.build(verify, manifest, prior=prior, generated_at=GENERATED_AT)

    assert page.since is not None
    assert page.new_points == [dropped.id]
    assert page.removed_points == []
    trends = [t.trend for t in page.posture.tiles()]
    assert any(t is not None for t in trends)
    marked = next(p for p in page.graph.points if p.id == dropped.id)
    assert marked.is_new is True


# --------------------------------------------------------------------------
# The renderer: data blocks into the committed template.
# --------------------------------------------------------------------------

# Strings that exist ONLY in the design artifact's example constants. Values
# that also occur in a real fixture (alex's own resource names) are deliberately
# not sentinels here: they would pass for the wrong reason.
EXAMPLE_DATA = [
    "delete_contact",
    "delete_company",
    "update_deal",
    "enrich_person",
    "get_platform_how_to_guide",
    "search_contacts",
    "aggregate_fundraising",
    "/api/accounts",
    "/api/instruments",
    "carta-crm",
    "v1.4.0",
    "683d29c",
    "Designed · awaiting your data",
    "ISOLATED",
    "GATED · AUDITED",
    "FILTER CONTAINED",
    "one role, twelve grants",
    "stages 6–7",
]


def blocks(html: str) -> dict:
    """Extract the emitted JS data blocks back out of a rendered page.

    Structure is pinned on these, not on the HTML: a CSS or markup change must
    not break a data test, and a data change must not pass one.
    """
    from itest.report import render as render_module

    return render_module.extract_blocks(html)


@pytest.fixture
def rendered_s7(alex_s7) -> str:
    from itest.report import render as render_module

    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    return render_module.render(page)


def test_render_produces_a_self_contained_page(rendered_s7: str) -> None:
    assert rendered_s7.lstrip().startswith("<title>")
    # No marker survives: every injection point was filled.
    assert "itest:data:" not in rendered_s7
    assert "<script" in rendered_s7 and "</script>" in rendered_s7


def test_no_example_data_survives_rendering(rendered_s7: str) -> None:
    """The template's design-artifact sample data never reaches output."""
    for sample in EXAMPLE_DATA:
        assert sample not in rendered_s7, sample


def test_every_data_block_is_emitted(rendered_s7: str) -> None:
    data = blocks(rendered_s7)
    assert set(data) == {
        "PAGE",
        "TOOLS",
        "TOOLPOSTURE",
        "SWEEP",
        "POSTURE",
        "POINTS",
        "CHAIN",
    }


def test_rendered_numbers_trace_to_verify_json(alex_s7) -> None:
    """Every headline number is the verify JSON field it claims to be."""
    from itest.report import render as render_module

    verify, manifest = alex_s7
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    data = blocks(render_module.render(page))

    nums = {n["label"]: n for n in data["PAGE"]["verdict"]["nums"]}
    assert nums["integrations verified"]["value"] == verify["passing"]
    assert nums["integrations verified"]["total"] == verify["total_points"]
    assert nums["drift"]["value"] == verify["orphaned_tests"] + len(
        verify["unregistered"]
    )
    assert data["PAGE"]["verdict"]["word"] == "AT RISK"
    assert len(data["POINTS"]) == sum(
        1 for p in manifest.points if p.type == "iam_edge"
    )
    assert len(data["CHAIN"]) == sum(
        1 for p in manifest.points if p.type == "event_edge"
    )


def test_sweep_has_one_status_column_when_probe_kind_is_unknown(
    rendered_s7: str,
) -> None:
    data = blocks(rendered_s7)
    assert data["PAGE"]["api"]["columns"] == ["Status"]
    for group in data["SWEEP"]:
        for endpoint in group["eps"]:
            assert len(endpoint["cells"]) == 1
            assert endpoint["cells"][0]["txt"] == "STUB"


def test_absent_tools_render_a_named_empty_state(rendered_s7: str) -> None:
    data = blocks(rendered_s7)
    assert data["TOOLS"] == []
    assert data["TOOLPOSTURE"] == []
    assert "No agent tools declared" in data["PAGE"]["toolBand"]["word"] or (
        "No agent tools declared" in data["PAGE"]["tools"]["emptyTitle"]
    )
    # Named, not invented: nothing claims a tool was verified.
    assert data["PAGE"]["verdict"]["nums"][0]["label"] != "agent tools verified"


def test_absent_not_analyzed_renders_a_named_empty_state(rendered_s7: str) -> None:
    data = blocks(rendered_s7)
    assert data["PAGE"]["notAnalyzed"]["present"] is False
    assert data["PAGE"]["notAnalyzed"]["emptyTitle"]


def test_tool_ledger_renders_groups_rows_and_exceptions(alex_s7) -> None:
    from itest.report import render as render_module

    verify, manifest = alex_s7
    verify["tools"] = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    data = blocks(render_module.render(page))

    groups = {g["g"]: g for g in data["TOOLS"]}
    assert set(groups) == {"Destructive", "Writes"}
    assert [r["n"] for r in groups["Destructive"]["rows"]] == ["delete_record"]
    # Columns are the traits actually checked, not a fixed set.
    assert [h["slug"] for h in groups["Destructive"]["headers"]] == [
        "authority.anonymous",
        "blast.mutation_class",
        "change.schema_drift",
        "change.description_drift",
    ]
    assert [h["slug"] for h in groups["Writes"]["headers"]] == [
        "authority.anonymous",
        "change.description_drift",
    ]
    changed = groups["Writes"]["rows"][0]
    assert changed["flag"] is True
    assert {c["txt"] for c in changed["cells"]} == {"PASS", "CHANGED"}
    # The exception is carried through with its diff, never summarized away.
    exception = data["PAGE"]["tools"]["exceptions"][0]
    assert exception["tag"] == "changed"
    assert "update_record" in exception["title"]
    assert data["PAGE"]["toolBand"]["word"] == "TOOLS AT RISK"
    assert len(data["TOOLPOSTURE"]) == 4


def test_since_toggles_trend_markup(tmp_path, monkeypatch) -> None:
    from itest.report import render as render_module

    project = synced(tmp_path, monkeypatch, ALEX_S7)
    verify = verify_json(project)
    manifest = load_manifest(project / ".itest" / "manifest.yaml")
    prior = manifest.model_copy(deep=True)
    dropped = next(p for p in prior.points if p.type == "iam_edge")
    prior.points = [p for p in prior.points if p.id != dropped.id]

    without = blocks(
        render_module.render(
            report_model.build(verify, manifest, generated_at=GENERATED_AT)
        )
    )
    with_since = blocks(
        render_module.render(
            report_model.build(verify, manifest, prior=prior, generated_at=GENERATED_AT)
        )
    )

    # Off by default: no trend key at all, and no since-line.
    assert all(tile.get("trend") is None for tile in without["POSTURE"])
    assert without["PAGE"]["verdict"]["since"] is None
    assert without["PAGE"]["posture"]["since"] == ""
    # On with --since.
    assert any(tile.get("trend") for tile in with_since["POSTURE"])
    assert with_since["PAGE"]["verdict"]["since"]["added"] == 1
    assert sum(1 for p in with_since["POINTS"] if p["isNew"]) == 1


def test_graph_survives_a_single_point_and_no_chain(alex_s7) -> None:
    """The diagram's geometry must not divide by zero on thin data."""
    from itest.report import render as render_module

    verify, manifest = alex_s7
    keep = next(p for p in manifest.points if p.type == "iam_edge")
    verify["points"] = [p for p in verify["points"] if p["id"] == keep.id]
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    data = blocks(render_module.render(page))

    assert len(data["POINTS"]) == 1
    assert data["CHAIN"] == []


def test_rendered_page_escapes_data_into_the_script_block(alex_s7) -> None:
    """A resource name cannot close the script tag or inject markup."""
    from itest.report import render as render_module

    verify, manifest = alex_s7
    verify["points"][0]["target"] = "</script><img src=x onerror=alert(1)>"
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    html = render_module.render(page)

    assert "</script><img" not in html
    assert html.count("</script>") == 1


# --------------------------------------------------------------------------
# End to end: the CLI, and a snapshot of every page's data blocks.
# --------------------------------------------------------------------------

SNAPSHOTS = FIXTURES / "report" / "snapshots"
REFERENCE_API = FIXTURES / "report" / "reference-api-state.json"


def stable(data: dict) -> dict:
    """Drop the two values that legitimately differ between runs."""
    footer = [
        f for f in data["PAGE"]["footer"] if f["k"] not in ("elapsed", "generated")
    ]
    data["PAGE"]["footer"] = footer
    return data


def snapshot(name: str, data: dict) -> None:
    """Compare against the committed snapshot, or write it the first time."""
    path = SNAPSHOTS / f"{name}.json"
    actual = json.dumps(stable(data), indent=2, sort_keys=True) + "\n"
    if not path.exists():  # pragma: no cover - only on a new snapshot
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        pytest.fail(f"Wrote a new snapshot at {path}; re-run to check it.")
    assert actual == path.read_text(encoding="utf-8"), (
        f"{name} data blocks changed. If intended, delete {path} and re-run."
    )


def render_project(project: Path, **kwargs) -> dict:
    from itest.report import render as render_module

    verify = verify_json(project)
    verify["elapsed_seconds"] = 0.0
    manifest = load_manifest(project / ".itest" / "manifest.yaml")
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT, **kwargs)
    return blocks(render_module.render(page))


def test_snapshot_alex_stub_only_run(tmp_path, monkeypatch) -> None:
    """A stub-only run renders honestly: named, counted, and not green."""
    project = synced(tmp_path, monkeypatch, ALEX_S7)
    data = render_project(project)

    assert data["PAGE"]["verdict"]["word"] == "AT RISK"
    snapshot("alex-s7", data)


def test_snapshot_reference_api_sweep(tmp_path, monkeypatch) -> None:
    """The reference API's seven routes become a grouped endpoint sweep."""
    project = synced(tmp_path, monkeypatch, REFERENCE_API)
    data = render_project(project)

    resources = [g["res"] for g in data["SWEEP"]]
    assert resources == ["/health", "/leaky", "/public", "/secured"]
    assert sum(len(g["eps"]) for g in data["SWEEP"]) == 7
    snapshot("reference-api", data)


def test_snapshot_tool_ledger_page(tmp_path, monkeypatch) -> None:
    """A verify JSON carrying the P31 tool ledger renders the tools sections."""
    from itest.report import render as render_module

    project = synced(tmp_path, monkeypatch, REFERENCE_API)
    verify = verify_json(project)
    verify["elapsed_seconds"] = 0.0
    verify["tools"] = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    manifest = load_manifest(project / ".itest" / "manifest.yaml")
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    data = blocks(render_module.render(page))

    assert data["PAGE"]["toolBand"]["word"] == "TOOLS AT RISK"
    assert len(data["TOOLS"]) == 2
    snapshot("tool-ledger", data)


def test_cli_report_writes_a_page(tmp_path, monkeypatch) -> None:
    synced(tmp_path, monkeypatch, ALEX_S7)
    out = tmp_path / "readiness.html"

    result = runner.invoke(app, ["report", "--html", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "Wrote" in result.output
    # The verdict never becomes an exit code; that is verify's job.
    assert "AT RISK" in result.output
    assert "itest:data:" not in out.read_text(encoding="utf-8")


def test_cli_report_reads_a_verify_json_with_from(tmp_path, monkeypatch) -> None:
    project = synced(tmp_path, monkeypatch, ALEX_S7)
    document = verify_json(project)
    source = tmp_path / "verify.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    out = tmp_path / "from.html"

    result = runner.invoke(
        app, ["report", "--html", "--from", str(source), "--out", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert blocks(out.read_text(encoding="utf-8"))["PAGE"]["verdict"]["word"]


def test_cli_report_redacts_account_ids(tmp_path, monkeypatch) -> None:
    """--redact pseudonymizes through verify's own scrubber, not a new one."""
    project = synced(tmp_path, monkeypatch, ALEX_S7)
    document = verify_json(project)
    # The alex fixture already ships a pseudonymous account; give it a real
    # shaped one so there is something for the scrubber to do.
    raw = json.dumps(document).replace("111111111111", "987654321098")
    source = tmp_path / "verify.json"
    source.write_text(raw, encoding="utf-8")
    out = tmp_path / "redacted.html"

    result = runner.invoke(
        app,
        ["report", "--html", "--from", str(source), "--out", str(out), "--redact"],
    )

    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    data = blocks(html)
    assert "987654321098" not in html
    account = next(f for f in data["PAGE"]["footer"] if f["k"] == "account")
    assert account["v"] == "111111111111"
    assert "--redact" in next(f for f in data["PAGE"]["footer"] if f["k"] == "run")["v"]


def test_cli_report_since_adds_trends(tmp_path, monkeypatch) -> None:
    project = synced(tmp_path, monkeypatch, ALEX_S7)
    manifest_file = project / ".itest" / "manifest.yaml"
    prior_file = tmp_path / "prior.yaml"
    prior = load_manifest(manifest_file)
    prior.points = [p for p in prior.points if p.type != "event_edge"]
    prior_file.write_text(
        yaml.safe_dump(prior.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    out = tmp_path / "since.html"

    plain = runner.invoke(app, ["report", "--out", str(tmp_path / "plain.html")])
    assert plain.exit_code == 0, plain.output
    result = runner.invoke(
        app, ["report", "--out", str(out), "--since", str(prior_file)]
    )
    assert result.exit_code == 0, result.output

    without = blocks((tmp_path / "plain.html").read_text(encoding="utf-8"))
    with_since = blocks(out.read_text(encoding="utf-8"))
    assert without["PAGE"]["verdict"]["since"] is None
    assert with_since["PAGE"]["verdict"]["since"]["added"] == 1
    assert any(tile.get("trend") for tile in with_since["POSTURE"])


def test_cli_report_without_a_manifest_exits_2(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["report", "--html"])
    assert result.exit_code == 2
    assert "No manifest" in result.output
