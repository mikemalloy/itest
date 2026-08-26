"""Regression: verify must map node ids when an ancestor holds a pytest config.

Found live on the first iam_edge/event_edge run: the customer's terraform
directory sat under a parent with ``pyproject.toml``, so pytest inferred that
parent as rootdir and reported node ids as ``sub/dir/itest_tests/...``. The
manifest records ``itest_tests/...`` and every test looked "missing" — 14
passing tests reported as 14 stubs.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from itest.cli import app

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
SKIP_LINE = 'pytest.skip("stub: implement this integration test")'

runner = CliRunner()


def test_verify_maps_nodeids_under_parent_pyproject(tmp_path, monkeypatch) -> None:
    # Parent carries a Python config file, as a monorepo's terraform/ dir might.
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    project = tmp_path / "stacks" / "6_agents"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output

    stub = project / "itest_tests" / "test_sg_edges.py"
    text = stub.read_text()
    i = text.index("def test_sg_web_to_db_5432():")
    j = text.index(SKIP_LINE, i)
    end = text.index("\n", j) + 1
    stub.write_text(text[:j] + "assert 1 + 1 == 2\n" + text[end:])

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "1 passing" in result.output, result.output
    assert "[PASS] aws_security_group.web -> aws_security_group.db" in result.output
    # Nothing may fall through as unregistered because of a rootdir prefix.
    assert "Unregistered tests" not in result.output, result.output
