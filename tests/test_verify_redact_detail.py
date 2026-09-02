"""F1 regression: `verify --redact` must scrub token-grade secrets in detail.

`--redact` applied only the account pseudonymizer, so a secret in a failing
test's assertion message or traceback (`TestResult.detail`) printed verbatim in
human, JSON, and JUnit output — while the "Tip: use --redact before sharing"
implied the output was safe. This pins that a leaked token AND an account id are
both scrubbed from detail in every output mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
STUB_FILE = "itest_tests/test_sg_edges.py"
SKIP_LINE = 'pytest.skip("stub: implement this integration test")'

# A credential-shaped token (AWS access key id) and a 12-digit account id.
LEAKED_TOKEN = "AKIAJ1234567890ABCDE"
LEAKED_ACCOUNT = "123456789012"


@pytest.fixture
def project_with_leaky_failure(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    # Turn one stub into a failure whose message leaks a token and an account.
    path = tmp_path / STUB_FILE
    text = path.read_text()
    marker = "def test_sg_web_to_db_5432():"
    i = text.index(marker)
    j = text.index(SKIP_LINE, i)
    end = text.index("\n", j) + 1
    statement = f'assert False, "leaked {LEAKED_TOKEN} for account {LEAKED_ACCOUNT}"'
    path.write_text(text[:j] + statement + "\n" + text[end:])
    return tmp_path


def test_redact_scrubs_token_and_account_in_human_detail(
    project_with_leaky_failure: Path,
) -> None:
    result = runner.invoke(app, ["verify", "--redact"])
    assert result.exit_code == 1, result.output
    assert LEAKED_TOKEN not in result.output
    assert LEAKED_ACCOUNT not in result.output
    # The account still shows as its stable pseudonym, so the report is usable.
    assert "111111111111" in result.output


def test_redact_scrubs_token_and_account_in_json_detail(
    project_with_leaky_failure: Path,
) -> None:
    result = runner.invoke(app, ["verify", "--redact", "--output", "json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    blob = json.dumps(payload)
    assert LEAKED_TOKEN not in blob
    assert LEAKED_ACCOUNT not in blob
    # Prove it is the detail field specifically that was scrubbed.
    detail = next(
        t["detail"]
        for t in payload["tests"]
        if t["canonical"].endswith("::test_sg_web_to_db_5432")
    )
    assert LEAKED_TOKEN not in detail
    assert LEAKED_ACCOUNT not in detail


def test_redact_scrubs_token_and_account_in_junit_detail(
    project_with_leaky_failure: Path,
) -> None:
    result = runner.invoke(app, ["verify", "--redact", "--output", "junit"])
    assert result.exit_code == 1, result.output
    junit = (project_with_leaky_failure / "itest-results.xml").read_text(
        encoding="utf-8"
    )
    assert LEAKED_TOKEN not in junit
    assert LEAKED_ACCOUNT not in junit


def test_without_redact_the_secret_is_left_alone(
    project_with_leaky_failure: Path,
) -> None:
    """The default must not silently rewrite output; --redact is the opt-in."""
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1, result.output
    assert LEAKED_TOKEN in result.output
    assert LEAKED_ACCOUNT in result.output
