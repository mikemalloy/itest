"""`itest traits` and `itest recipes`: the table and its recipes, from the CLI.

Both read only the manifest and the table. No server is contacted — the probe
module is poisoned for the whole of each test to prove it — so either command
is safe to run anywhere, including against a checkout whose servers are down.

The human output is snapshotted against the reference declaration, so a table
edit (a new row, a moved rule, a different recipe) is a visible diff here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from test_declarations_plan_sync import _sync, make_workdir, runner

from itest.cli import app
from itest.core.declarations.tools import tool_point_id
from itest.core.declarations.traits import load_traits, trait_table_hash

SNAPSHOTS = Path(__file__).resolve().parent / "fixtures" / "traits" / "snapshots"


def snapshot(name: str, actual: str) -> None:
    """Compare against the committed snapshot, or write it the first time.

    Never writes under CI: a missing snapshot there is a failure, not a new
    baseline nobody reviewed.
    """
    path = SNAPSHOTS / f"{name}.txt"
    if not path.exists() and os.environ.get("CI"):
        pytest.fail(f"Missing snapshot {path}; generate it locally and commit it.")
    if not path.exists():  # pragma: no cover - only on a new snapshot
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        pytest.fail(f"Wrote a new snapshot at {path}; re-run to check it.")
    assert actual == path.read_text(encoding="utf-8"), (
        f"{name} output changed. If intended, delete {path} and re-run."
    )


@pytest.fixture
def synced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The reference declaration, synced once, then every server made unreachable
    in-process: from here on nothing may import or call the probe."""
    workdir = make_workdir(tmp_path, monkeypatch)
    assert _sync().exit_code == 0
    monkeypatch.setitem(sys.modules, "itest.probes.mcp", None)
    return workdir


def _traits(*args: str):
    return runner.invoke(app, ["traits", *args])


# --- the table ------------------------------------------------------------------


def test_traits_prints_the_table(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no manifest, no declaration: the table alone
    monkeypatch.setitem(sys.modules, "itest.probes.mcp", None)
    result = _traits()
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == (
        f"Trait table {trait_table_hash()}: 15 traits in 4 families "
        "(10 engine, 5 generated)."
    )
    b2 = next(
        line for line in lines if line.lstrip().startswith("blast.destructive_gating ")
    )
    for column in (
        "Blast radius",
        "BLAST-2",
        "generated",
        "active",
        "mutation == destructive",
        "tool_gating.md",
    ):
        assert column in b2
    snapshot("table", result.output)


def test_traits_json_is_the_table(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = _traits("--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    table = load_traits()
    assert payload["trait_table_hash"] == trait_table_hash(table)
    assert payload["families"] == table.families
    assert [t["id"] for t in payload["traits"]] == table.ids
    assert payload["traits"][0] == {
        "id": "authority.anonymous",
        "family": "authority",
        "family_name": "Authority",
        "code": "AUTH-1",
        "name": "refuses anonymous",
        "kind": "engine",
        "tier": "readonly",
        "applies_when": "transport.kind == http or auth.enforced_over_stdio",
        "standards": ["ASI03", "LLM02", "semgrep-server-4", "CWE-306"],
        "recipe": "tool_authn.md",
    }


# --- one tool ---------------------------------------------------------------------


def test_traits_for_a_tool_decides_every_trait(synced: Path) -> None:
    result = _traits("--for", "reference-mcp/delete_record")
    assert result.exit_code == 0, result.output
    out = result.output
    point_id = tool_point_id("reference-mcp", "delete_record")
    assert out.splitlines()[0] == (
        "reference-mcp/delete_record: destructive (detected) [approval confirm_param]"
    )
    assert f"id={point_id}" in out
    assert "11 of 15 traits apply" in out  # no anonymous (stdio), no observed
    lines = out.splitlines()
    assert any(
        line.lstrip().startswith("APPLIES")
        and "blast.destructive_gating" in line
        and "destructive gating" in line
        for line in lines
    )
    assert "rule: mutation == destructive" in out
    assert any(
        line.lstrip().startswith("does not apply")
        and "authority.delegation" in line
        and "caller identity passthrough" in line
        for line in lines
    )
    assert "rule: identity.runs_as == passthrough" in out
    snapshot("for-delete-record", out)


def test_traits_for_names_a_declared_override(synced: Path, monkeypatch) -> None:
    from test_declarations_plan_sync import _declare

    monkeypatch.delitem(sys.modules, "itest.probes.mcp")
    _declare(
        synced,
        tools={
            "get_guide": {
                "traits": ["none-of-these"],
                "notes": "Static prose. Nothing to authorise, mutate or leak.",
            }
        },
    )
    assert _sync().exit_code == 0
    monkeypatch.setitem(sys.modules, "itest.probes.mcp", None)
    result = _traits("--for", "reference-mcp/get_guide")
    assert result.exit_code == 0, result.output
    assert "0 of 15 traits apply" in result.output
    assert "declared traits: none-of-these" in result.output


def test_traits_for_json(synced: Path) -> None:
    result = _traits("--for", "reference-mcp/get_guide", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["server"] == "reference-mcp"
    assert payload["tool"] == "get_guide"
    assert payload["point_id"] == tool_point_id("reference-mcp", "get_guide")
    assert payload["trait_table_hash"] == trait_table_hash()
    assert payload["table_changed_since_sync"] is False
    applies = {t["id"]: t["applies"] for t in payload["traits"]}
    assert [k for k, v in applies.items() if v] == [
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "blast.mutation_class_observed",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ]
    a2 = next(t for t in payload["traits"] if t["id"] == "authority.tenant_isolation")
    assert a2["reason"] == (
        "rule: auth.second_tenant_env present and mutation != informational"
    )
    assert a2["kind"] == "generated"


def test_traits_for_an_unknown_tool_names_the_known_ones(synced: Path) -> None:
    result = _traits("--for", "reference-mcp/no_such_tool")
    assert result.exit_code == 1
    assert "no_such_tool" in result.output
    for known in ("delete_record", "enrich", "get_guide"):
        assert known in result.output


def test_traits_for_an_unknown_server_names_the_known_ones(synced: Path) -> None:
    result = _traits("--for", "other-mcp/get_guide")
    assert result.exit_code == 1
    assert "other-mcp" in result.output
    assert "reference-mcp" in result.output


def test_traits_for_needs_server_slash_tool(synced: Path) -> None:
    result = _traits("--for", "delete_record")
    assert result.exit_code == 1
    assert "<server>/<tool>" in result.output


def test_traits_for_without_a_manifest_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = _traits("--for", "reference-mcp/delete_record")
    assert result.exit_code == 1
    assert "No manifest" in result.output


# --- recipes ----------------------------------------------------------------------


def _recipes(*args: str):
    return runner.invoke(app, ["recipes", *args])


def test_recipes_lists_every_recipe_the_table_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recipe files are the skill's, and may land later: a missing one is a
    line that says so, never an error."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "itest.probes.mcp", None)
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "tool_authn.md").write_text("# authn\n", encoding="utf-8")

    result = _recipes("--recipes-dir", "recipes")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Recipes the trait table references, in recipes:"
    authn = next(line for line in lines if "tool_authn.md" in line)
    assert "present" in authn and "authority.anonymous" in authn
    containment = next(line for line in lines if "tool_containment.md" in line)
    assert "missing" in containment
    assert (
        "containment.parameter_scope, containment.expression_passthrough, "
        "containment.output_hygiene"
    ) in containment
    assert lines[-1] == "9 recipe(s): 1 present, 8 missing."
    snapshot("recipes", result.output)


def test_recipes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = _recipes("--recipes-dir", "recipes", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    by_name = {r["recipe"]: r for r in payload["recipes"]}
    assert set(by_name) == {t.recipe for t in load_traits().traits}
    assert by_name["tool_identity.md"] == {
        "recipe": "tool_identity.md",
        "path": "recipes/tool_identity.md",
        "exists": False,
        "traits": ["authority.backing_least_privilege", "authority.delegation"],
    }


def test_recipes_default_search_finds_a_checkout_of_the_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --recipes-dir the project's own copy of the skill is searched
    first; HOME is moved so a globally installed skill cannot leak in."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skill = tmp_path / "skills" / "itest-implementer" / "references" / "recipes"
    skill.mkdir(parents=True)
    (skill / "tool_gating.md").write_text("# gating\n", encoding="utf-8")
    result = _recipes()
    assert result.exit_code == 0, result.output
    assert "in skills/itest-implementer/references/recipes:" in result.output
    gating = next(line for line in result.output.splitlines() if "tool_gating" in line)
    assert "present" in gating


# --- slugs, codes and standards -------------------------------------------------


def test_the_table_shows_slug_code_family_kind_tier_rule_standards_recipe(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = _traits()
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    header = lines[2].split()
    assert header[:2] == ["SLUG", "CODE"]
    assert " ".join(header) == (
        "SLUG CODE FAMILY KIND TIER APPLIES WHEN STANDARDS RECIPE"
    )
    anonymous = next(
        line for line in lines if line.lstrip().startswith("authority.anonymous ")
    )
    assert "AUTH-1" in anonymous
    assert "ASI03, LLM02, semgrep-server-4, CWE-306" in anonymous
    gating = next(
        line for line in lines if line.lstrip().startswith("blast.destructive_gating ")
    )
    assert "BLAST-2" in gating and "ASI02, LLM03" in gating


def test_traits_json_carries_code_and_standards(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    payload = json.loads(_traits("--json").output)
    by_id = {t["id"]: t for t in payload["traits"]}
    assert by_id["change.description_drift"]["code"] == "CHANGE-3"
    assert by_id["change.description_drift"]["standards"] == [
        "ASI04",
        "ASI01",
        "LLM01",
        "semgrep-client-2",
    ]
    assert by_id["blast.egress"]["standards"] == ["ASI04", "LLM02", "semgrep-server-21"]


def test_traits_for_shows_each_decision_with_its_rule_and_standards(
    synced: Path,
) -> None:
    result = _traits("--for", "reference-mcp/get_guide")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    anonymous = next(line for line in lines if "authority.anonymous " in line)
    assert anonymous.lstrip().startswith("does not apply")  # stdio, no own check
    assert "AUTH-1" in anonymous
    assert "rule: transport.kind == http or auth.enforced_over_stdio" in anonymous
    assert "ASI03, LLM02, semgrep-server-4, CWE-306" in anonymous
    isolation = next(line for line in lines if "authority.tenant_isolation " in line)
    assert isolation.lstrip().startswith("does not apply")
    assert "rule: auth.second_tenant_env present" in isolation

    payload = json.loads(_traits("--for", "reference-mcp/get_guide", "--json").output)
    first = payload["traits"][0]
    assert (first["id"], first["code"]) == ("authority.anonymous", "AUTH-1")
    assert first["standards"] == ["ASI03", "LLM02", "semgrep-server-4", "CWE-306"]


# --- authority.anonymous and the transport ------------------------------------------

ANONYMOUS_RULE = "rule: transport.kind == http or auth.enforced_over_stdio"


def test_traits_for_says_why_a_stdio_server_gets_no_anonymous_check(
    synced: Path,
) -> None:
    """Over stdio the process boundary is the authentication boundary, so the
    check does not apply — and the reader is told which rule decided."""
    result = _traits("--for", "reference-mcp/get_guide")
    assert result.exit_code == 0, result.output
    line = next(
        line for line in result.output.splitlines() if "authority.anonymous " in line
    )
    assert line.lstrip().startswith("does not apply")
    assert ANONYMOUS_RULE in line


def test_traits_for_shows_the_anonymous_check_when_stdio_enforcement_is_declared(
    synced: Path, monkeypatch
) -> None:
    from test_declarations_plan_sync import _declare

    monkeypatch.delitem(sys.modules, "itest.probes.mcp")
    _declare(
        synced,
        auth={
            "scheme": "bearer",
            "credential_env": "REFERENCE_MCP_TOKEN",
            "enforced_over_stdio": True,
        },
    )
    assert _sync().exit_code == 0
    monkeypatch.setitem(sys.modules, "itest.probes.mcp", None)
    result = _traits("--for", "reference-mcp/get_guide")
    assert result.exit_code == 0, result.output
    line = next(
        line for line in result.output.splitlines() if "authority.anonymous " in line
    )
    assert line.lstrip().startswith("APPLIES")
    assert ANONYMOUS_RULE in line
