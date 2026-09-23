"""``itest sync`` reads the declared sources last and prints one line each.

The line is the evidence lane's own vocabulary — a run, rows, tools matched and
unmatched, STALE — never pass, fail, verified or covered. A source that cannot
be read, a server the manifest does not inventory, and a source file that does
not parse are each a line with the reason; sync exits 0 regardless, because
evidence is best-effort and never blocking.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from test_declarations_plan_sync import _sync, make_workdir
from test_evidence_promptfoo import FIXTURE

from itest.core.manifest import SourceRecord, load_manifest
from itest.core.syncer import render_evidence_line

RUN = "run eval-zyk-2026-09-23T18:26:29 at 2026-09-23T18:26:29.565Z"


def _source(workdir: Path, name: str, document: dict | str) -> Path:
    path = workdir / ".itest" / "sources" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    path.write_text(text, encoding="utf-8")
    return path


def _promptfoo(workdir: Path, **changes: object) -> dict:
    results = workdir / "evidence" / "promptfoo.json"
    results.parent.mkdir(exist_ok=True)
    shutil.copy(FIXTURE, results)
    return {
        "kind": "promptfoo",
        "server": "reference-mcp",
        "results": "evidence/promptfoo.json",
        "agent": "sonnet-5 via agent.js",
        "standards": ["ASI01"],
        **changes,
    }


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = make_workdir(tmp_path, monkeypatch)
    assert _sync().exit_code == 0
    return workdir


def _lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("evidence ")]


def test_a_no_op_sync_without_sources_leaves_the_manifest_byte_identical(
    workdir: Path,
) -> None:
    manifest_file = workdir / ".itest" / "manifest.yaml"
    before = manifest_file.read_bytes()
    result = _sync()
    assert result.exit_code == 0, result.output
    assert _lines(result.output) == []
    assert manifest_file.read_bytes() == before


def test_sync_prints_one_line_per_source_and_records_the_lane(
    workdir: Path,
) -> None:
    _source(workdir, "promptfoo-lab", _promptfoo(workdir))
    result = _sync()
    assert result.exit_code == 0, result.output
    assert _lines(result.output) == [
        f"evidence promptfoo-lab (promptfoo): {RUN}, 12 rows, 2 tools matched, "
        "0 unmatched"
    ]
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    (source,) = manifest.sources
    assert source.status == "read"
    assert source.matched_tools == ["get_guide", "search_records"]
    assert {r.point_id for r in manifest.evidence} == {
        p.id for p in manifest.points if p.target in source.matched_tools
    }
    # The lane is top level and every record carries the sync's clock.
    assert all(
        r.recorded_at == manifest.evidence[0].recorded_at for r in manifest.evidence
    )


def test_a_stale_run_says_so_on_the_line(workdir: Path) -> None:
    _source(workdir, "promptfoo-lab", _promptfoo(workdir, max_age_days=0))
    result = _sync()
    assert result.exit_code == 0, result.output
    (line,) = _lines(result.output)
    assert line.endswith(", 0 unmatched, STALE")
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    assert manifest.sources[0].stale is True
    assert all(r.stale for r in manifest.evidence)


def test_a_missing_results_file_is_unreadable_and_sync_goes_on(
    workdir: Path,
) -> None:
    _source(workdir, "ci", _promptfoo(workdir, results="evidence/absent.json"))
    result = _sync()
    assert result.exit_code == 0, result.output
    assert _lines(result.output) == [
        "evidence ci (promptfoo): unreadable: results file not found: "
        "evidence/absent.json"
    ]
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    assert manifest.evidence == []
    (source,) = manifest.sources
    assert source.status == "unreadable"
    assert source.kind == "promptfoo" and source.server == "reference-mcp"


def test_a_results_file_that_is_not_promptfoo_output_is_unreadable(
    workdir: Path,
) -> None:
    _source(workdir, "ci", _promptfoo(workdir))
    (workdir / "evidence" / "promptfoo.json").write_text(
        json.dumps({"hello": "world"}), encoding="utf-8"
    )
    result = _sync()
    assert result.exit_code == 0, result.output
    (line,) = _lines(result.output)
    assert line.startswith("evidence ci (promptfoo): unreadable: ")
    assert "results.results" in line


def test_a_server_the_manifest_does_not_inventory_joins_nothing(
    workdir: Path,
) -> None:
    _source(workdir, "elsewhere", _promptfoo(workdir, server="other-server"))
    result = _sync()
    assert result.exit_code == 0, result.output
    assert _lines(result.output) == [
        f"evidence elsewhere (promptfoo): {RUN}, 12 rows, server other-server "
        "not declared; nothing joined"
    ]
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    assert manifest.evidence == []
    assert manifest.sources[0].status == "server_not_declared"


def test_a_source_file_that_does_not_parse_is_a_line_beside_the_others(
    workdir: Path,
) -> None:
    _source(workdir, "promptfoo-lab", _promptfoo(workdir))
    _source(workdir, "broken", {"kind": "promptfoo", "server": "reference-mcp"})
    result = _sync()
    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert len(lines) == 2
    assert lines[0].startswith("evidence broken (unknown kind): unreadable: ")
    assert "exactly one" in lines[0]
    assert lines[1].startswith("evidence promptfoo-lab (promptfoo): run ")
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    assert [s.status for s in manifest.sources] == ["unreadable", "read"]
    assert manifest.sources[0].kind is None
    assert len(manifest.evidence) == 2


def test_removing_every_source_clears_the_lane(workdir: Path) -> None:
    path = _source(workdir, "promptfoo-lab", _promptfoo(workdir))
    assert _sync().exit_code == 0
    path.unlink()
    result = _sync()
    assert result.exit_code == 0, result.output
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    assert manifest.evidence == [] and manifest.sources == []
    text = (workdir / ".itest" / "manifest.yaml").read_text(encoding="utf-8")
    assert "\nevidence:" not in text and "\nsources:" not in text


def test_the_line_never_borrows_the_boundary_lanes_words() -> None:
    for line in (
        SourceRecord(name="a", kind="promptfoo", server="s", status="read", rows=3),
        SourceRecord(name="b", status="unreadable", reason="gone"),
        SourceRecord(
            name="c", kind="promptfoo", server="s", status="server_not_declared"
        ),
    ):
        text = render_evidence_line(line).lower()
        for word in ("pass", "fail", "verified", "covered"):
            assert word not in text, (line.status, word)
