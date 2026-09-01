from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
STUB_FILE = "itest_tests/test_sg_edges.py"

SKIP_LINE = 'pytest.skip("stub: implement this integration test")'


@pytest.fixture
def synced_project(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    return tmp_path


def _set_body(base_dir: Path, func: str, statement: str) -> None:
    """Replace the pytest.skip in ``func`` with ``statement`` (no indent)."""
    path = base_dir / STUB_FILE
    text = path.read_text()
    marker = f"def {func}():"
    i = text.index(marker)
    j = text.index(SKIP_LINE, i)
    end = text.index("\n", j) + 1
    path.write_text(text[:j] + statement + "\n" + text[end:])


def test_verify_rollup_with_one_passing(synced_project: Path) -> None:
    # Turn the web->db stub into a real (pure-python) passing check.
    _set_body(synced_project, "test_sg_web_to_db_5432", "assert 1 + 1 == 2")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert (
        "3 integration points: 1 passing, 0 failing, 0 errored, 2 stubs"
        in result.output
    )
    # The passing point renders as PASS, the stubs as STUB.
    assert "[PASS] aws_security_group.web -> aws_security_group.db" in result.output
    assert result.output.count("[STUB]") == 2


def test_verify_failing_test_exit_1(synced_project: Path) -> None:
    _set_body(synced_project, "test_sg_alb_to_web_80", "assert False, 'boom'")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1, result.output
    assert "1 failing" in result.output
    assert "[FAIL] aws_security_group.alb -> aws_security_group.web" in result.output
    assert "Failing tests:" in result.output


def test_verify_json_output(synced_project: Path) -> None:
    _set_body(synced_project, "test_sg_web_to_db_5432", "assert True")
    result = runner.invoke(app, ["verify", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total_points"] == 3
    assert payload["passing"] == 1
    assert payload["stubs"] == 2


def test_verify_junit_output(synced_project: Path) -> None:
    result = runner.invoke(app, ["verify", "--output", "junit"])
    assert result.exit_code == 0
    junit = synced_project / "itest-results.xml"
    assert junit.exists()
    content = junit.read_text()
    assert "<testsuite" in content
    assert "test_sg_internet_to_alb_443" in content


def test_verify_without_manifest_is_config_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2
    assert "itest plan" in result.output


def test_verify_collection_error_is_not_reported_as_stub(
    synced_project: Path,
) -> None:
    """A module that fails to import is an error, not an untouched stub.

    Regression: a collection error produces no per-test outcome, so every test
    in the module fell through to "missing" and its point rendered [STUB].
    verify then looked like a clean, merely-unimplemented suite while the
    environment was actually broken — and exited 0.
    """
    stub = synced_project / STUB_FILE
    # Stand in for a dependency missing from the venv (boto3, say).
    stub.write_text("import itest_missing_dep_xyz\n" + stub.read_text())

    result = runner.invoke(app, ["verify"])

    # A broken environment is a config problem, not a clean run.
    assert result.exit_code == 2, result.output
    assert "[ERROR]" in result.output
    assert "[STUB]" not in result.output
    assert "3 errored" in result.output
    # The error text is shown, so the cause is actionable.
    assert "Errored tests" in result.output
    assert "itest_missing_dep_xyz" in result.output


def test_verify_collection_error_in_json_output(synced_project: Path) -> None:
    stub = synced_project / STUB_FILE
    stub.write_text("import itest_missing_dep_xyz\n" + stub.read_text())

    result = runner.invoke(app, ["verify", "--output", "json"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["errored"] == 3
    assert payload["stubs"] == 0
    assert all(p["status"] == "error" for p in payload["points"])


def test_verify_without_pytest_fails_fast(synced_project: Path, monkeypatch) -> None:
    """Missing pytest gets a clear message, not an opaque traceback."""
    from itest.core import verifier

    monkeypatch.setattr(verifier, "_pytest_installed", lambda: False)

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2, result.output
    assert "pytest is not installed" in result.output
    assert "pip install" in result.output


# --------------------------------------------------------------------------
# Point status precedence: fail > error > pass > stub
# --------------------------------------------------------------------------


def _register_second_test(base_dir: Path, path_rel: str, name: str) -> str:
    """Register a second test against the web->db point. Returns its canonical."""
    from itest.core.manifest import TestEntry, load_manifest, save_manifest

    manifest_path = base_dir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_path)
    web_db = next(t for t in manifest.tests if t.test_name == "test_sg_web_to_db_5432")
    manifest.tests.append(
        TestEntry(
            id=f"t-{name}",
            point_id=web_db.point_id,
            path=path_rel,
            test_name=name,
            ownership_hash="0" * 64,
            status="implemented",
        )
    )
    save_manifest(manifest, manifest_path)
    return f"{path_rel}::{name}"


def _force_outcomes(monkeypatch, outcomes: dict, collection_errors: dict) -> None:
    """Drive the real rollup with a chosen pytest result set.

    A collection error aborts pytest's whole session, so a module that errors
    and a sibling test that passes cannot both be produced by an actual run.
    Substituting the runner exercises run_verify's rollup — the code under
    test — against the mix that a --continue-on-collection-errors run, or a
    partially broken suite, would hand it.
    """
    from itest.core import verifier

    monkeypatch.setattr(
        verifier,
        "_run_pytest",
        lambda base_dir, junit, gating=None: (outcomes, collection_errors),
    )


def test_errored_sibling_is_not_masked_by_a_pass(
    synced_project: Path, monkeypatch
) -> None:
    """Regression: [PASS] won as soon as anything passed and nothing failed.

    A sibling test that could not run at all was invisible, and verify exited 0
    reporting the point as covered.
    """
    extra_rel = "itest_tests/test_extra_web_db.py"
    _register_second_test(synced_project, extra_rel, "test_extra_web_db")

    _force_outcomes(
        monkeypatch,
        outcomes={
            "itest_tests/test_sg_edges.py::test_sg_web_to_db_5432": {
                "outcome": "passed",
                "detail": "",
                "duration": 0.01,
            }
        },
        collection_errors={
            extra_rel: {"outcome": "error", "detail": "ImportError: boto3"}
        },
    )

    result = runner.invoke(app, ["verify"])

    assert result.exit_code == 2, result.output
    assert "[ERROR] aws_security_group.web -> aws_security_group.db" in result.output
    assert "[PASS]" not in result.output
    assert "1 errored" in result.output


def test_failing_outranks_errored(synced_project: Path, monkeypatch) -> None:
    """A real failure is the more actionable signal, so it wins."""
    extra_rel = "itest_tests/test_extra_web_db.py"
    _register_second_test(synced_project, extra_rel, "test_extra_web_db")

    _force_outcomes(
        monkeypatch,
        outcomes={
            "itest_tests/test_sg_edges.py::test_sg_web_to_db_5432": {
                "outcome": "failed",
                "detail": "boom",
                "duration": 0.01,
            }
        },
        collection_errors={
            extra_rel: {"outcome": "error", "detail": "ImportError: boto3"}
        },
    )

    result = runner.invoke(app, ["verify"])

    assert result.exit_code == 1, result.output
    assert "[FAIL] aws_security_group.web -> aws_security_group.db" in result.output
    assert "1 failing" in result.output


def test_all_errored_still_reports_error(synced_project: Path, monkeypatch) -> None:
    """The pre-existing all-error case keeps working."""
    _force_outcomes(
        monkeypatch,
        outcomes={},
        collection_errors={
            "itest_tests/test_sg_edges.py": {
                "outcome": "error",
                "detail": "ImportError: boto3",
            }
        },
    )

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2, result.output
    assert "3 errored" in result.output


# --------------------------------------------------------------------------
# Point tags are type-aware
# --------------------------------------------------------------------------

ALEX_S6 = REPO_ROOT / "tests" / "fixtures" / "alex" / "alex-s6.json"


@pytest.fixture
def alex_project(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX_S6)])
    return tmp_path


def test_point_lines_are_type_aware(alex_project: Path) -> None:
    """Regression: every point line appended "({protocol}:{ports})".

    Only sg_edge carries those attributes, so IAM and event points rendered a
    bare "(:)" — the tag slot was there but empty for two thirds of what ITest
    detects.
    """
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output

    assert "(:)" not in result.output

    planner_line = next(
        line
        for line in result.output.splitlines()
        if "aws_sqs_queue.analysis_jobs -> aws_lambda_function.planner" in line
    )
    assert "event_source_mapping" in planner_line


def test_sg_point_lines_carry_the_plan_tag(synced_project: Path) -> None:
    """verify prints the same tag plan does, direction included."""
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "aws_security_group.web -> aws_security_group.db (tcp:5432 ingress)" in (
        result.output
    )


# --------------------------------------------------------------------------
# --redact: verify output is safe to paste
# --------------------------------------------------------------------------

FAKE_ACCOUNT = "999988887777"


def _inject_external_target(base_dir: Path) -> str:
    """Point one manifest point at an ARN carrying a distinct fake account.

    The alex fixtures are already pseudonymized, so asserting against them
    could not tell redaction from a fixture that was clean to begin with.
    """
    from itest.core.manifest import load_manifest, save_manifest

    manifest_path = base_dir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_path)
    arn = f"arn:aws:sqs:us-west-1:{FAKE_ACCOUNT}:private-queue"
    manifest.points[0].target = arn
    save_manifest(manifest, manifest_path)
    return arn


def test_verify_redact_pseudonymizes_account_ids(alex_project: Path) -> None:
    _inject_external_target(alex_project)

    result = runner.invoke(app, ["verify", "--redact"])
    assert result.exit_code == 0, result.output

    assert FAKE_ACCOUNT not in result.output
    assert "111111111111" in result.output


def test_verify_without_redact_leaves_account_ids_alone(alex_project: Path) -> None:
    """The default must not silently rewrite what the user is looking at."""
    _inject_external_target(alex_project)

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert FAKE_ACCOUNT in result.output


def test_verify_redact_correlates_repeated_accounts(alex_project: Path) -> None:
    """The same account in two places maps to the same pseudonym."""
    result = runner.invoke(app, ["verify", "--redact"])
    assert result.exit_code == 0, result.output

    # alex-s6's ARNs all carry one account, so exactly one pseudonym appears.
    pseudonyms = set(re.findall(r"\b(\d)\1{11}\b", result.output))
    assert pseudonyms == {"1"}, f"expected one account pseudonym, saw {pseudonyms}"


def test_verify_redact_json_output(alex_project: Path) -> None:
    _inject_external_target(alex_project)

    result = runner.invoke(app, ["verify", "--redact", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    blob = json.dumps(payload)
    assert FAKE_ACCOUNT not in blob
    assert "111111111111" in blob
    # Structure survives: ids, counts and statuses are untouched.
    assert payload["total_points"] == 14
    assert len(payload["points"]) == 14


def test_verify_redact_leaves_ids_and_test_names_alone(alex_project: Path) -> None:
    """Point ids, HCL addresses and test names carry no account IDs."""
    plain = json.loads(runner.invoke(app, ["verify", "--output", "json"]).output)
    redacted = json.loads(
        runner.invoke(app, ["verify", "--redact", "--output", "json"]).output
    )

    assert [p["id"] for p in redacted["points"]] == [p["id"] for p in plain["points"]]
    assert [t["canonical"] for t in redacted["tests"]] == [
        t["canonical"] for t in plain["tests"]
    ]


def test_verify_redact_junit_output(alex_project: Path) -> None:
    _inject_external_target(alex_project)

    result = runner.invoke(app, ["verify", "--redact", "--output", "junit"])
    assert result.exit_code == 0, result.output

    junit = (alex_project / "itest-results.xml").read_text(encoding="utf-8")
    assert FAKE_ACCOUNT not in junit


def test_verify_hints_at_redact_when_targets_are_arns(alex_project: Path) -> None:
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "Tip: use --redact before sharing this output" in result.output


def test_verify_does_not_hint_when_already_redacted(alex_project: Path) -> None:
    result = runner.invoke(app, ["verify", "--redact"])
    assert result.exit_code == 0, result.output
    assert "Tip:" not in result.output


def test_verify_does_not_hint_without_arn_targets(synced_project: Path) -> None:
    """The web-app fixture has no ARN targets, so no hint is warranted."""
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "Tip:" not in result.output
