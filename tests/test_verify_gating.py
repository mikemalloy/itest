"""`itest verify` gates tests by the resolved environment's tier policy.

A tier the environment disallows is not run and not even collected: a whole
gated file is ignored (never imported), an individual gated test in a mixed
file is deselected. Its point reports ``[GATED <env>]`` and the rollup gains
``, N gated`` — but only when N > 0, so a project with no policy prints exactly
what it always did, byte for byte, and gating alone never changes the exit code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core import verifier
from itest.core.manifest import load_manifest, save_manifest

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
STUB_FILE = "itest_tests/test_sg_edges.py"

POLICY = """\
version: 1
environments:
  dev:    { tiers: [static, readonly, active] }
  stage:  { tiers: [static, readonly, active] }
  prod:   { tiers: [static, readonly], production: true }
"""

# Today's pinned rollup for the freshly synced simple-web-app (DESIGN.md).
TODAY_ROLLUP = (
    "3 integration points: 0 passing, 0 failing, 0 errored, 3 stubs, 0 orphaned tests."
)


@pytest.fixture
def synced(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    return tmp_path


def _write_policy(base: Path) -> None:
    (base / ".itest" / "environments.yaml").write_text(POLICY, encoding="utf-8")


def _bind(base: Path, name: str) -> None:
    (base / ".itest" / "environment").write_text(name + "\n", encoding="utf-8")


def _set_tier(base: Path, test_name: str, tier: str) -> None:
    mpath = base / ".itest" / "manifest.yaml"
    manifest = load_manifest(mpath)
    entry = next(t for t in manifest.tests if t.test_name == test_name)
    entry.tier = tier
    save_manifest(manifest, mpath)


def _set_all_tiers(base: Path, tier: str) -> None:
    mpath = base / ".itest" / "manifest.yaml"
    manifest = load_manifest(mpath)
    for entry in manifest.tests:
        entry.tier = tier
    save_manifest(manifest, mpath)


# --------------------------------------------------------------------------
# Backward compatibility: no policy file == today, byte for byte
# --------------------------------------------------------------------------


def test_no_policy_file_output_is_byte_identical_to_today(synced: Path) -> None:
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert TODAY_ROLLUP in result.output
    assert "gated" not in result.output
    assert "[GATED" not in result.output
    assert "safe floor" not in result.output


def test_no_policy_file_does_not_gate_an_active_test_from_running(synced: Path) -> None:
    """No policy withholds active, but the pinned fixture has no active tests,
    so this project's three readonly stubs still run exactly as before."""
    _set_tier(synced, "test_sg_web_to_db_5432", "readonly")
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert result.output.count("[STUB]") == 3


# --------------------------------------------------------------------------
# The AND: policy allows AND binding selects an allowing environment
# --------------------------------------------------------------------------


def test_active_runs_when_bound_to_an_allowing_environment(synced: Path) -> None:
    _write_policy(synced)
    _bind(synced, "dev")
    _set_tier(synced, "test_sg_web_to_db_5432", "active")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    # dev allows active, so nothing is gated: three stubs, as usual.
    assert "[GATED" not in result.output
    assert "gated" not in result.output
    assert result.output.count("[STUB]") == 3


def test_active_is_gated_when_bound_to_a_disallowing_environment(synced: Path) -> None:
    _write_policy(synced)
    _bind(synced, "prod")
    _set_tier(synced, "test_sg_web_to_db_5432", "active")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "[GATED prod] aws_security_group.web -> aws_security_group.db" in (
        result.output
    )
    assert ", 1 gated" in result.output
    # The gated point is not also counted as a stub.
    assert "2 stubs" in result.output
    assert result.output.count("[STUB]") == 2


def test_environment_flag_beats_the_binding_file(synced: Path) -> None:
    _write_policy(synced)
    _bind(synced, "prod")
    _set_tier(synced, "test_sg_web_to_db_5432", "active")

    result = runner.invoke(app, ["verify", "--environment", "dev"])
    assert result.exit_code == 0, result.output
    assert "[GATED" not in result.output


def test_gated_points_are_listed_after_the_others(synced: Path) -> None:
    _write_policy(synced)
    _bind(synced, "prod")
    _set_tier(synced, "test_sg_internet_to_alb_443", "active")

    result = runner.invoke(app, ["verify"])
    lines = [ln for ln in result.output.splitlines() if ln.startswith("  [")]
    assert lines[-1].startswith("  [GATED prod]"), lines
    assert all("[GATED" not in ln for ln in lines[:-1]), lines


# --------------------------------------------------------------------------
# Deselected, not skipped: a gated test must never import
# --------------------------------------------------------------------------


def test_a_fully_gated_file_is_never_imported(synced: Path) -> None:
    """Every test in the file is gated, so the file is ignored, not collected.

    Import-time code would fire the moment pytest imported the module; a
    deselect (as opposed to an ignore) would still import it. The marker's
    absence is the proof that nothing in the file ran or even loaded.
    """
    _write_policy(synced)
    _bind(synced, "prod")
    _set_all_tiers(synced, "active")

    stub = synced / STUB_FILE
    stub.write_text(
        'import pathlib; pathlib.Path("IMPORTED.marker").write_text("x")\n'
        + stub.read_text()
    )

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert not (synced / "IMPORTED.marker").exists()
    assert ", 3 gated" in result.output
    assert result.output.count("[GATED prod]") == 3
    assert "[STUB]" not in result.output


def test_a_mixed_file_deselects_only_the_gated_test(synced: Path) -> None:
    """One active test among readonly siblings: it is deselected, they run."""
    _write_policy(synced)
    _bind(synced, "prod")
    _set_tier(synced, "test_sg_web_to_db_5432", "active")

    result = runner.invoke(app, ["verify", "--output", "json"])
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    by_name = {t["canonical"].split("::")[-1]: t for t in payload["tests"]}
    assert by_name["test_sg_web_to_db_5432"]["outcome"] == "gated"
    assert payload["gated"] == 1
    # The gated test is not tallied as a stub.
    assert payload["stubs"] == 2


# --------------------------------------------------------------------------
# The safe floor: policy present, nothing bound
# --------------------------------------------------------------------------


def test_policy_present_no_binding_announces_the_safe_floor(synced: Path) -> None:
    _write_policy(synced)
    _set_tier(synced, "test_sg_web_to_db_5432", "active")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "safe floor" in result.output
    # Active is gated on the floor, but with no environment named it is bare.
    assert "[GATED] aws_security_group.web -> aws_security_group.db" in result.output
    assert ", 1 gated" in result.output


# --------------------------------------------------------------------------
# Exit codes and error handling
# --------------------------------------------------------------------------


def test_gating_alone_does_not_change_exit_code(synced: Path) -> None:
    _write_policy(synced)
    _bind(synced, "prod")
    _set_all_tiers(synced, "active")

    result = runner.invoke(app, ["verify"])
    # Everything gated, nothing ran, nothing failed: still a clean exit.
    assert result.exit_code == 0, result.output


def test_a_failing_allowed_test_still_exits_1_despite_gating(synced: Path) -> None:
    _write_policy(synced)
    _bind(synced, "prod")
    _set_tier(synced, "test_sg_web_to_db_5432", "active")  # gated

    # Make an allowed (readonly) test fail.
    path = synced / STUB_FILE
    text = path.read_text()
    skip = 'pytest.skip("stub: implement this integration test")'
    i = text.index("def test_sg_alb_to_web_80():")
    j = text.index(skip, i)
    end = text.index("\n", j) + 1
    path.write_text(text[:j] + "assert False, 'boom'\n" + text[end:])

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1, result.output
    assert ", 1 gated" in result.output
    assert "1 failing" in result.output


def test_a_bad_policy_refuses_to_start_with_exit_2(synced: Path) -> None:
    """production + active is refused at load, so verify never runs the suite."""
    (synced / ".itest" / "environments.yaml").write_text(
        "version: 1\nenvironments:\n"
        "  prod: { tiers: [static, readonly, active], production: true }\n",
        encoding="utf-8",
    )
    _bind(synced, "prod")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2, result.output
    assert "active" in result.output


def test_binding_an_undefined_environment_is_exit_2(synced: Path) -> None:
    _write_policy(synced)
    result = runner.invoke(app, ["verify", "--environment", "ghost"])
    assert result.exit_code == 2, result.output
    assert "ghost" in result.output


def _report_with(point_status: str) -> verifier.VerifyReport:
    """One point, one gated test on it, point status as given."""
    return verifier.VerifyReport(
        total_points=1,
        stubs=1 if point_status == "stub" else 0,
        gated=1 if point_status == "gated" else 0,
        environment="prod",
        points=[
            verifier.PointResult(
                id="p1", source="a", target="b", status=point_status, tag="t"
            )
        ],
        tests=[
            verifier.TestResult(
                canonical="itest_tests/t.py::test_x",
                outcome="gated",
                point_id="p1",
                detail="",
            )
        ],
    )


def test_partially_gated_tests_are_announced() -> None:
    """A gated test on a point that still reports other coverage must not
    vanish silently: the point shows its remaining status, and one line
    names how many tests the environment withheld."""
    text = verifier.render_human(_report_with("stub"))
    assert (
        "1 gated test(s) withheld by this environment on points that still "
        "report their remaining tests." in text
    )


def test_fully_gated_points_do_not_double_report() -> None:
    """A fully-gated point already announces itself as [GATED]; its tests
    must not also count toward the partial-withholding line."""
    text = verifier.render_human(_report_with("gated"))
    assert "withheld" not in text
