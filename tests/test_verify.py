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
        verifier, "_run_pytest", lambda base_dir, junit: (outcomes, collection_errors)
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
