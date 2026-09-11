"""A declarations-only project plans without Terraform.

An MCP server is not in Terraform state, so a project that only declares
servers has nothing for ``terraform show -json`` to read. Plan used to run it
anyway and abort on the empty state (or on terraform not being installed)
before a single declaration was read. The rule now:

- no ``--tf-json``, no ``*.tf`` in the project directory, and a declaration
  under ``.itest/tools/`` — the Terraform side is an empty resource set,
  whether terraform is absent, fails, or reports an empty state;
- ``*.tf`` present and an empty state is still the "you forgot to apply" error,
  with its message unchanged;
- no ``*.tf`` and no declaration is still an error: there is nothing to plan.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from test_declarations_plan_sync import EXPECTED_TOOLS, make_workdir, runner

from itest.cli import app
from itest.core.manifest import load_manifest
from itest.core.planner import EMPTY_TERRAFORM, load_plan_json

#: What `terraform show -json` prints for a directory with no state.
EMPTY_STATE_OUTPUT = '{"format_version":"1.0","terraform_version":"1.9.0"}'


def _fake_terraform(bin_dir: Path, *, stdout: str = "", exit_code: int = 0) -> None:
    """A `terraform` on PATH that prints ``stdout`` and exits ``exit_code``.

    Shell builtins only: PATH holds nothing but this directory.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "terraform"
    script.write_text(
        f"#!/bin/sh\necho '{stdout}'\nexit {exit_code}\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with the reference declaration, a policy, and no ``*.tf``."""
    (tmp_path / "project").mkdir()
    workdir = make_workdir(tmp_path / "project", monkeypatch)
    (workdir / "state.json").unlink()  # nothing Terraform-shaped at all
    return workdir


def _path_with(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    """PATH holding only ``bin_dir``: a fake terraform, or no terraform at all.

    The declaration's argv is an absolute interpreter path, so the server still
    launches with nothing else on PATH.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(bin_dir))


def _plan_json() -> dict:
    result = runner.invoke(app, ["plan", "--output", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _tool_names(payload: dict) -> set[str]:
    return {p["target"] for p in payload["new_points"] if p["type"] == "mcp_tool"}


def test_plan_without_terraform_installed_plans_the_declared_tools(
    declared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path_with(monkeypatch, tmp_path / "empty-bin")
    assert _tool_names(_plan_json()) == EXPECTED_TOOLS


def test_plan_with_an_empty_terraform_state_plans_the_declared_tools(
    declared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_terraform(tmp_path / "bin", stdout=EMPTY_STATE_OUTPUT)
    _path_with(monkeypatch, tmp_path / "bin")
    assert _tool_names(_plan_json()) == EXPECTED_TOOLS


def test_plan_when_terraform_finds_no_state_plans_the_declared_tools(
    declared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_terraform(tmp_path / "bin", stdout="No state.", exit_code=1)
    _path_with(monkeypatch, tmp_path / "bin")
    assert _tool_names(_plan_json()) == EXPECTED_TOOLS


def test_sync_from_a_declarations_only_directory_writes_the_tool_points(
    declared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path_with(monkeypatch, tmp_path / "empty-bin")
    result = runner.invoke(app, ["sync", "--auto-approve"])
    assert result.exit_code == 0, result.output
    manifest = load_manifest(declared / ".itest" / "manifest.yaml")
    assert {p.target for p in manifest.points if p.type == "mcp_tool"} == (
        EXPECTED_TOOLS
    )


def test_terraform_files_with_an_empty_state_are_still_an_error(
    declared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real "you forgot to apply" case keeps its message."""
    (declared / "main.tf").write_text("terraform {}\n", encoding="utf-8")
    _fake_terraform(tmp_path / "bin", stdout=EMPTY_STATE_OUTPUT)
    _path_with(monkeypatch, tmp_path / "bin")
    result = runner.invoke(app, ["plan"])
    assert result.exit_code == 1
    assert "is an empty state" in result.output
    assert "applied" in result.output


def test_terraform_files_without_terraform_installed_are_still_an_error(
    declared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (declared / "main.tf").write_text("terraform {}\n", encoding="utf-8")
    _path_with(monkeypatch, tmp_path / "empty-bin")
    result = runner.invoke(app, ["plan"])
    assert result.exit_code == 1
    assert "terraform not found" in result.output


def test_a_directory_with_neither_terraform_nor_declarations_is_still_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _path_with(monkeypatch, tmp_path / "empty-bin")
    result = runner.invoke(app, ["plan"])
    assert result.exit_code == 1
    assert "terraform not found" in result.output


def test_the_empty_terraform_side_is_the_shape_the_tests_use(
    declared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path_with(monkeypatch, tmp_path / "empty-bin")
    assert load_plan_json(None, declared) == EMPTY_TERRAFORM
    assert EMPTY_TERRAFORM == {"values": {"root_module": {"resources": []}}}


def test_tf_json_still_wins_in_a_declarations_only_directory(
    declared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit file is read as before — including refusing a bad one."""
    _path_with(monkeypatch, tmp_path / "empty-bin")
    (declared / "bad.json").write_text('{"format_version": "1.0"}', encoding="utf-8")
    result = runner.invoke(app, ["plan", "--tf-json", "bad.json"])
    assert result.exit_code == 1
    assert "is an empty state" in result.output
    assert os.environ["PATH"] == str(tmp_path / "empty-bin")
