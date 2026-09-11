"""Generated code a human maintains is proportional to the facts only a human
could supply — never to the number of tools.

So, per declared server, sync writes:

- **one engine module per group** — active, or passive for every other
  tier (``test_<server>__engine.py`` and
  ``..._active.py``), ITest-owned and regenerated while it matches its ownership
  hash. It holds no per-tool code: one parametrized test reads the manifest at
  collection time and runs every engine trait of every tool, so a tool added to
  the manifest is picked up without regenerating anything;
- **one thin binding per (tool, generated trait)**: the frozen docstring (point,
  trait, schema hash), one import from ``itest.checks``, one call — no logic;
- **one conftest.py, written once and human-owned from birth**: the fixtures only
  a human can fill in. Never rewritten, and never ownership-hashed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_declarations_plan_sync import (
    ACTIVE_FILE,
    CONFTEST_FILE,
    ENGINE_ACTIVE_FILE,
    ENGINE_FILE,
    _edit_table,
    _functions,
    _sync,
    make_workdir,
    runner,
)

from itest.checks import CheckResult
from itest.cli import app
from itest.core import stubgen
from itest.core.declarations.tools import tool_point_id
from itest.core.declarations.traits import load_traits
from itest.core.manifest import load_manifest, save_manifest
from itest.traits import runtime
from itest.traits.ids import trait_ident


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return make_workdir(tmp_path, monkeypatch)


def _manifest(workdir: Path):
    return load_manifest(workdir / ".itest" / "manifest.yaml")


def _collect(workdir: Path, path: str) -> list[str]:
    """Node ids pytest collects from one file, run the way verify runs it."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            path,
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--rootdir={workdir}",
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode in (0, 5), result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if "::" in line]


# --- the generated binding ---------------------------------------------------------


def test_a_generated_stub_is_exactly_the_binding(workdir: Path) -> None:
    assert _sync().exit_code == 0
    manifest = _manifest(workdir)
    point = next(p for p in manifest.points if p.target == "delete_record")
    schema = point.attributes["schema_hash"]
    text = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    block = stubgen.render_generated_stub(
        point,
        "test_delete_record__blast__destructive_gating",
        load_traits().get("blast.destructive_gating"),
    )
    assert block in text
    assert block == (
        "\n\ndef test_delete_record__blast__destructive_gating("
        "itest_target, itest_point, blast__destructive_gating_fixtures):\n"
        f'    """itest point: {point.id}  trait: blast.destructive_gating  '
        f'schema: {schema}"""\n'
        "    from itest.checks import run_generated_check\n"
        "\n"
        "    result = run_generated_check(\n"
        '        "blast.destructive_gating", itest_point, itest_target, '
        "fixtures=blast__destructive_gating_fixtures\n"
        "    )\n"
        '    assert result.status == "pass", result.detail\n'
    )
    # No logic, no skip line, nothing per tool beyond the binding.
    assert "pytest.skip" not in text
    assert text.startswith(stubgen.GENERATED_HEADER)


def test_generated_bindings_name_the_tool_then_the_trait(workdir: Path) -> None:
    assert _sync().exit_code == 0
    active = _functions(workdir / ACTIVE_FILE)
    assert "test_get_guide__authority__backing_least_privilege" in active
    assert "test_delete_record__blast__destructive_gating" in active
    assert len(active) == 19


# --- the engine module ------------------------------------------------------------


def test_the_engine_module_is_one_parametrized_test_per_tier(workdir: Path) -> None:
    assert _sync().exit_code == 0
    text = (workdir / ENGINE_FILE).read_text(encoding="utf-8")
    assert text == stubgen.render_engine_module("reference-mcp", "passive")
    assert _functions(workdir / ENGINE_FILE) == {"test_engine"}
    # Nothing in it names a tool: the manifest is the only list of tools.
    for tool in ("delete_record", "get_guide", "enrich"):
        assert tool not in text
    active = (workdir / ENGINE_ACTIVE_FILE).read_text(encoding="utf-8")
    assert active == stubgen.render_engine_module("reference-mcp", "active")


def test_the_engine_module_parametrizes_over_the_manifest(workdir: Path) -> None:
    assert _sync().exit_code == 0
    table = load_traits()
    manifest = _manifest(workdir)
    expected = sorted(
        f"{ENGINE_FILE}::test_engine[{p.target}-{trait}]"
        for p in manifest.points
        for trait in p.traits_planned
        if table.get(trait).kind == "engine" and table.get(trait).tier == "readonly"
    )
    assert sorted(_collect(workdir, ENGINE_FILE)) == expected
    assert len(expected) == 48

    # Every case is registered in the manifest, so verify maps its result.
    registered = {
        t.canonical for t in manifest.tests if t.path == ENGINE_FILE and not t.retired
    }
    assert registered == set(expected)


def test_a_tool_added_to_the_manifest_is_picked_up_without_regeneration(
    workdir: Path,
) -> None:
    assert _sync().exit_code == 0
    engine_before = (workdir / ENGINE_FILE).read_text(encoding="utf-8")
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    template = next(p for p in manifest.points if p.target == "get_guide")
    manifest.points.append(
        template.model_copy(
            update={
                "id": tool_point_id("reference-mcp", "brand_new"),
                "target": "brand_new",
                "traits_planned": ["authority.anonymous", "change.inventory"],
            }
        )
    )
    save_manifest(manifest, manifest_file)

    collected = _collect(workdir, ENGINE_FILE)
    assert f"{ENGINE_FILE}::test_engine[brand_new-authority.anonymous]" in collected
    assert f"{ENGINE_FILE}::test_engine[brand_new-change.inventory]" in collected
    assert (workdir / ENGINE_FILE).read_text(encoding="utf-8") == engine_before


def test_a_hand_edited_engine_module_is_frozen_and_reported(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _sync().exit_code == 0
    edited = (workdir / ENGINE_FILE).read_text(encoding="utf-8") + "# mine\n"
    (workdir / ENGINE_FILE).write_text(edited, encoding="utf-8")

    _edit_table(tmp_path, monkeypatch, B3="always")  # something for sync to apply
    result = _sync()
    assert result.exit_code == 0, result.output
    assert "human-modified file(s) preserved" in result.output
    assert "0 human-modified" not in result.output
    assert (workdir / ENGINE_FILE).read_text(encoding="utf-8") == edited


def test_an_itest_owned_engine_module_is_regenerated(
    workdir: Path, monkeypatch
) -> None:
    """While the file still matches its ownership hash it is ITest's, so a new
    template (an ITest upgrade) replaces it on the next applied sync."""
    assert _sync().exit_code == 0
    monkeypatch.setattr(
        stubgen, "ENGINE_HEADER", stubgen.ENGINE_HEADER + "# template v2\n"
    )
    manifest_file = workdir / ".itest" / "manifest.yaml"
    document = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    document.pop("trait_table_hash")  # force an applied sync
    manifest_file.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    (workdir / ".itest" / "plan.json").unlink()

    result = _sync()
    assert result.exit_code == 0, result.output
    text = (workdir / ENGINE_FILE).read_text(encoding="utf-8")
    assert "# template v2" in text
    hashes = {
        t.ownership_hash for t in _manifest(workdir).tests if t.path == ENGINE_FILE
    }
    assert hashes == {stubgen.file_hash(workdir / ENGINE_FILE)}


# --- the human-owned conftest ------------------------------------------------------


def test_the_conftest_is_written_once_and_never_rewritten(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _sync().exit_code == 0
    conftest = workdir / CONFTEST_FILE
    text = conftest.read_text(encoding="utf-8")
    assert "yours" in text.lower()
    # One fixture per generated trait in the table, each a clear placeholder.
    for trait in (
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "authority.delegation",
        "blast.destructive_gating",
        "blast.audit",
    ):
        assert f"def {trait_ident(trait)}_fixtures(" in text
    assert "from itest.traits.runtime import itest_point, itest_target" in text

    mine = text.replace(
        "def authority__tenant_isolation_fixtures(itest_point):",
        "def authority__tenant_isolation_fixtures(itest_point):"
        "  # filled in by a human",
    )
    conftest.write_text(mine, encoding="utf-8")
    _edit_table(tmp_path, monkeypatch, A2="always")  # an applied sync
    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 1 stub(s)" in result.output
    assert conftest.read_text(encoding="utf-8") == mine


def test_ownership_hashes_cover_stubs_and_the_engine_module_not_conftest(
    workdir: Path,
) -> None:
    assert _sync().exit_code == 0
    manifest = _manifest(workdir)
    paths = {t.path for t in manifest.tests}
    assert paths == {ACTIVE_FILE, ENGINE_FILE, ENGINE_ACTIVE_FILE}
    assert CONFTEST_FILE not in paths
    for path in paths:
        hashes = {t.ownership_hash for t in manifest.tests if t.path == path}
        assert hashes == {stubgen.file_hash(workdir / path)}


def test_the_generated_suite_runs_and_nothing_is_an_error(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine checks now run for real against reference-mcp over stdio. That
    transport has no guard, so A1 fails exactly the five read tools (an anonymous
    call answered); the three mutating tools' A1 is not_verifiable (deferred to
    the active tier) and skips. With no credential exported the run is
    anonymous, and B1/D1-D3 pass on the anonymous listing for every tool; the
    traits the library has no check for skip; every generated binding skips on
    its unfilled fixture. Nothing is an error, and nothing else passes."""
    monkeypatch.delenv("REFERENCE_MCP_TOKEN", raising=False)
    assert _sync().exit_code == 0
    result = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json"]
    )
    assert result.exit_code == 1, result.output  # the real A1 findings
    report = json.loads(result.output)
    assert report["errored"] == 0
    failed = sorted(t["canonical"] for t in report["tests"] if t["outcome"] == "failed")
    assert failed == sorted(
        f"{ENGINE_FILE}::test_engine[{tool}-authority.anonymous]"
        for tool in (
            "get_guide",
            "search_records",
            "fetch_record",
            "lookalike_read",
            "enrich",
        )
    )
    passed = {t["canonical"] for t in report["tests"] if t["outcome"] == "passed"}
    assert passed == {
        f"{ENGINE_FILE}::test_engine[{tool}-{trait}]"
        for tool in (
            "get_guide",
            "search_records",
            "fetch_record",
            "create_record",
            "update_record",
            "delete_record",
            "lookalike_read",
            "enrich",
        )
        for trait in (
            "blast.mutation_class",
            "change.inventory",
            "change.schema_drift",
            "change.description_drift",
        )
    }
    others = {
        t["outcome"]
        for t in report["tests"]
        if t["outcome"] not in ("failed", "passed")
    }
    assert others == {"skipped"}
    assert len(report["tests"]) == 81
    assert report["unregistered"] == []


# --- the runtime the generated code binds to -----------------------------------------


def test_run_engine_case_returns_the_check_result(monkeypatch) -> None:
    import itest.checks

    calls = []

    def fake(trait_id, point, target, *, authenticated):
        calls.append((trait_id, point["target"], target, authenticated))
        return CheckResult("pass", "ok", {"n": 1})

    monkeypatch.setattr(itest.checks, "run_engine_check", fake)
    case = runtime.EngineCase(
        point={"target": "t", "id": "x"}, trait="authority.anonymous"
    )
    recorded = []
    result = runtime.run_engine_case(
        case,
        "TARGET",
        authenticated=True,
        record=lambda name, value: recorded.append((name, value)),
    )
    assert result == CheckResult("pass", "ok", {"n": 1})
    assert calls == [("authority.anonymous", "t", "TARGET", True)]
    assert recorded == [
        ("itest_check", {"status": "pass", "detail": "ok", "evidence": {"n": 1}})
    ]


def test_an_unimplemented_engine_check_skips(monkeypatch) -> None:
    """The library answers a trait it has no check for with not_verifiable ("no
    engine check for <id>"), which the runtime records and skips: not run, never
    an error and never a pass. C3 is in the table and not in the library."""
    case = runtime.EngineCase(
        point={"target": "t", "id": "x"}, trait="containment.output_hygiene"
    )
    recorded = []
    with pytest.raises(pytest.skip.Exception) as excinfo:
        runtime.run_engine_case(
            case,
            "TARGET",
            authenticated=False,
            record=lambda name, value: recorded.append((name, value)),
        )
    assert "not_verifiable: no engine check for containment.output_hygiene" in str(
        excinfo.value
    )
    assert recorded[0][1]["status"] == "not_verifiable"


@pytest.mark.parametrize("status", ["changed", "not_verifiable"])
def test_a_finding_that_is_not_a_failure_is_recorded_then_skipped(
    monkeypatch, status: str
) -> None:
    """`changed` waits on a reviewer and `not_verifiable` could not be judged:
    neither is a failing check. The CheckResult is recorded (the ledger reports
    it as-is), and the test skips, so the point reads as unverified — AT RISK —
    rather than failing and blocking the release."""
    import itest.checks

    monkeypatch.setattr(
        itest.checks,
        "run_engine_check",
        lambda *a, **k: CheckResult(status, "description moved", None),
    )
    case = runtime.EngineCase(
        point={"target": "t", "id": "x"}, trait="change.description_drift"
    )
    recorded = []
    with pytest.raises(pytest.skip.Exception) as excinfo:
        runtime.run_engine_case(
            case,
            "TARGET",
            authenticated=True,
            record=lambda name, value: recorded.append((name, value)),
        )
    assert f"{status}: description moved" in str(excinfo.value)
    assert recorded[0][1]["status"] == status


def test_a_failing_check_still_fails(monkeypatch) -> None:
    import itest.checks

    monkeypatch.setattr(
        itest.checks,
        "run_engine_check",
        lambda *a, **k: CheckResult("critical", "anonymous call accepted", None),
    )
    case = runtime.EngineCase(
        point={"target": "t", "id": "x"}, trait="authority.anonymous"
    )
    result = runtime.run_engine_case(case, "TARGET", authenticated=False)
    assert result.status == "critical"


def test_one_passive_engine_module_collects_every_non_active_tier(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine modules are keyed by path — (server, active | passive) — not by
    tier. A static-tier and a readonly-tier engine trait on one server share the
    one passive module, and that module collects both; keyed by tier, the second
    tier's render overwrote the first's and its cases vanished."""
    _edit_table(tmp_path, monkeypatch, D1={"tier": "static"})
    assert _sync().exit_code == 0

    text = (workdir / ENGINE_FILE).read_text(encoding="utf-8")
    assert text == stubgen.render_engine_module("reference-mcp", "passive")
    ids = {
        case.id
        for case in runtime.engine_cases(
            str(workdir / ENGINE_FILE), "reference-mcp", "passive"
        )
    }
    assert {
        "get_guide-change.inventory",
        "get_guide-authority.anonymous",
        "delete_record-change.inventory",
    } <= ids
    assert not {
        i
        for i in ids
        if i.endswith(
            ("-containment.parameter_scope", "-containment.expression_passthrough")
        )
    }  # active stays apart

    entries = [t for t in _manifest(workdir).tests if t.path == ENGINE_FILE]
    assert {t.tier for t in entries} == {"static", "readonly"}
    assert {t.test_name for t in entries} == {f"test_engine[{i}]" for i in ids}


def test_authenticated_means_a_credential_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime passes authenticated=True only when the named credential
    actually resolves (shell, then .itest/.env). A named but unset credential is
    an anonymous run — reference-mcp over stdio is the normal case — and the
    library then runs the listing checks on the anonymous listing."""
    from itest.probes.mcp import McpTarget

    name = "ITEST_RUNTIME_RESOLVE_TOKEN"
    monkeypatch.setenv(name, "")  # recorded, so the test restores it either way
    monkeypatch.delenv(name)
    named = McpTarget(kind="stdio", command=["unused"], credential_env=name)

    assert runtime.credential_resolves(named, tmp_path) is False
    monkeypatch.setenv(name, "from-the-shell")
    assert runtime.credential_resolves(named, tmp_path) is True
    monkeypatch.delenv(name)
    (tmp_path / ".itest").mkdir()
    (tmp_path / ".itest" / ".env").write_text(f"{name}=from-the-file\n")
    assert runtime.credential_resolves(named, tmp_path) is True

    unnamed = McpTarget(kind="stdio", command=["unused"])
    assert runtime.credential_resolves(unnamed, tmp_path) is False
