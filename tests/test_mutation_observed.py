"""``blast.mutation_class_observed`` from the manifest to the page.

The agreement check passes ``lookalike_read`` by design; only behaviour catches
it. This is the active-tier row that does: it runs where the committed policy
allows ``active``, a production environment refuses it, and a critical result
blocks the release and is named among the exceptions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_declarations_plan_sync import _sync, make_workdir, runner

from itest.cli import app
from itest.core.declarations.traits import load_traits
from itest.core.manifest import load_manifest
from itest.report import model as report_model

TRAIT = "blast.mutation_class_observed"

POLICY_WITH_PROD = """\
version: 1
environments:
  staging: { tiers: [static, readonly, active] }
  prod:    { tiers: [static, readonly], production: true }
"""


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = make_workdir(tmp_path, monkeypatch)
    (workdir / ".itest" / "environments.yaml").write_text(
        POLICY_WITH_PROD, encoding="utf-8"
    )
    assert _sync().exit_code == 0
    return workdir


def _verify(environment: str) -> dict:
    result = runner.invoke(
        app, ["verify", "--environment", environment, "--output", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def _checks(ledger: dict) -> dict[tuple[str, str], dict]:
    return {
        (tool["name"], check["trait"]): check
        for server in ledger["servers"]
        for tool in server["tools"]
        for check in tool["checks"]
    }


def test_the_row_is_a_separate_active_engine_trait() -> None:
    row = load_traits().get(TRAIT)
    assert row is not None
    assert (row.code, row.family, row.kind, row.tier) == (
        "BLAST-1b",
        "blast",
        "engine",
        "active",
    )
    assert row.recipe == "tool_mutation_class.md"
    assert row.standards == ["ASI02", "LLM03"]
    assert row.applies_when == (
        "mutation in [read, informational] and observation.snapshot_tool present"
    )


def test_it_is_planned_for_read_tools_only_when_a_snapshot_tool_is_declared(
    workdir: Path,
) -> None:
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    planned = {p.target: p.traits_planned for p in manifest.points}
    for tool in (
        "get_guide",
        "search_records",
        "fetch_record",
        "lookalike_read",
        "enrich",
    ):
        assert TRAIT in planned[tool], tool
    for tool in ("create_record", "update_record", "delete_record"):
        assert TRAIT not in planned[tool], tool
    result = runner.invoke(app, ["traits", "--for", "reference-mcp/lookalike_read"])
    line = next(line for line in result.output.splitlines() if f"{TRAIT} " in line)
    assert line.lstrip().startswith("APPLIES")
    assert "BLAST-1b" in line and "observation.snapshot_tool present" in line


def test_in_staging_lookalike_read_is_critical_and_blocks_the_release(
    workdir: Path,
) -> None:
    payload = _verify("staging")
    checks = _checks(payload["tools"])
    caught = checks[("lookalike_read", TRAIT)]
    assert caught["status"] == "critical", caught
    assert "readOnlyHint" in caught["detail"]
    assert caught["test"].endswith(f"::test_engine[lookalike_read-{TRAIT}]")
    assert "engine_active" in caught["test"]
    for tool in ("get_guide", "search_records", "fetch_record", "enrich"):
        assert checks[(tool, TRAIT)]["status"] == "pass", tool
    # The agreement check still passes it: the two answers differ by design.
    assert checks[("lookalike_read", "blast.mutation_class")]["status"] == "pass"
    (server,) = payload["tools"]["servers"]
    assert server["summary"]["critical"] == 1
    kinds = [(e["kind"], e["tool"], e["trait"]) for e in server["exceptions"]]
    assert ("critical", "lookalike_read", TRAIT) in kinds
    page = report_model.build(
        payload, load_manifest(workdir / ".itest" / "manifest.yaml")
    )
    assert page.verdict.word == "BLOCKED"


def test_a_production_environment_holds_it_out(workdir: Path) -> None:
    """Proving a read tool mutates means causing that mutation once: the
    active tier never runs where the policy says production."""
    payload = _verify("prod")
    checks = _checks(payload["tools"])
    caught = checks[("lookalike_read", TRAIT)]
    assert caught["status"] == "held_out", caught
    assert "prod" in caught["detail"]
    assert any(t["outcome"] == "gated" for t in payload["tests"])
