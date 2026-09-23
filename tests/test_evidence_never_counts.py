"""THE test: evidence never counts.

The same project with and without an evidence source yields identical
``ToolLedger.verified()``, the same verdict, and the same standards rollup —
and so does the same project with the evidence marked stale. Nothing the
evidence lane adds may change any of those numbers, ever: a passing red-team
run can never turn a cell green, and a stale one can never turn it amber.

Built from ``examples/reference-mcp`` in a temporary checkout, synced and
verified against the real server over stdio, exactly as a user would run it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from test_evidence_promptfoo import FIXTURE
from test_reference_mcp_example import copy_example
from typer.testing import CliRunner

from itest.cli import app
from itest.core.manifest import load_manifest
from itest.report import model as report_model

runner = CliRunner()


def _sync() -> None:
    result = runner.invoke(app, ["sync", "--auto-approve", "--allow-unreachable"])
    assert result.exit_code == 0, result.output


def _verify() -> dict:
    result = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def _observe(example: Path) -> tuple[int, str, list[dict]]:
    """What the boundary lane says: VERIFIED count, verdict, standards rollup."""
    payload = _verify()
    ledger = report_model.ToolLedger.model_validate(payload["tools"])
    manifest = load_manifest(example / ".itest" / "manifest.yaml")
    page = report_model.build(payload, manifest)
    return ledger.verified, page.verdict.word, payload["tools"]["standards"]


def _declare_source(example: Path, max_age_days: int = 7) -> Path:
    results = example / "evidence" / "promptfoo.json"
    results.parent.mkdir(exist_ok=True)
    shutil.copy(FIXTURE, results)
    path = example / ".itest" / "sources" / "promptfoo-lab.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "kind": "promptfoo",
                "server": "reference-mcp",
                "results": "evidence/promptfoo.json",
                "agent": "sonnet-5 + haiku-4-5 via agent.js",
                "standards": ["ASI01", "LLM01"],
                "max_age_days": max_age_days,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.slow
def test_evidence_never_changes_verified_the_verdict_or_the_rollup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = copy_example(tmp_path / "reference-mcp")
    monkeypatch.chdir(example)
    monkeypatch.setenv("REFERENCE_MCP_TOKEN", "dry-run-token")
    monkeypatch.delenv("REFERENCE_MCP_OPEN_URL", raising=False)

    _sync()
    without = _observe(example)
    assert load_manifest(example / ".itest" / "manifest.yaml").evidence == []

    _declare_source(example)
    _sync()
    manifest = load_manifest(example / ".itest" / "manifest.yaml")
    assert manifest.evidence, "the source was read and joined"
    assert [s.status for s in manifest.sources] == ["read"]
    assert not any(r.stale for r in manifest.evidence)
    assert _observe(example) == without

    _declare_source(example, max_age_days=0)
    _sync()
    manifest = load_manifest(example / ".itest" / "manifest.yaml")
    assert manifest.evidence and all(r.stale for r in manifest.evidence)
    assert _observe(example) == without

    # And removing the source removes the lane without touching the numbers.
    (example / ".itest" / "sources" / "promptfoo-lab.yaml").unlink()
    _sync()
    manifest = load_manifest(example / ".itest" / "manifest.yaml")
    assert manifest.evidence == [] and manifest.sources == []
    assert _observe(example) == without


def test_the_boundary_lane_has_no_path_to_the_evidence_lane() -> None:
    """The verifier, the lifecycle vocabulary and the standards rollup never
    import the evidence package: nothing upstream of the join can depend on it."""
    import itest.core.lifecycle as lifecycle
    import itest.core.verifier as verifier
    import itest.traits.standards as standards

    for module in (verifier, lifecycle, standards):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "itest.core.evidence" not in source, module.__name__
    assert "evidence" not in lifecycle.CHECK_STATES
    assert "evidence" not in lifecycle.COUNTED_STATES
