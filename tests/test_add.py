"""`itest add`: register an existing test function onto an existing point.

The narrow, designed-day-one version — it registers a human-authored test onto
a point the manifest already knows, and never declares a new point. The new
entry is human-owned from birth, so a sync round-trip must leave it exactly
where it is: not rewritten, not relocated, not de-registered. Registered tests
then join verify exactly like synced ones, tier gating included.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core.manifest import load_manifest

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
PROBE_FILE = "itest_tests/test_http_probes.py"


@pytest.fixture
def synced(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    return tmp_path


def _a_point_id(base: Path) -> str:
    manifest = load_manifest(base / ".itest" / "manifest.yaml")
    return next(p.id for p in manifest.points if p.target == "aws_security_group.alb")


def _write_probe(base: Path, body: str = "    assert True") -> None:
    (base / "itest_tests").mkdir(parents=True, exist_ok=True)
    (base / PROBE_FILE).write_text(
        f"import pytest\n\n\ndef test_probe_health():\n{body}\n"
    )


def _add(base: Path, **overrides) -> object:
    args = {
        "point": _a_point_id(base),
        "file": PROBE_FILE,
        "function": "test_probe_health",
        "tier": "active",
    }
    args.update(overrides)
    return runner.invoke(
        app,
        [
            "add",
            "--point",
            args["point"],
            "--file",
            args["file"],
            "--function",
            args["function"],
            "--tier",
            args["tier"],
        ],
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_registers_an_existing_test_onto_a_point(synced: Path) -> None:
    _write_probe(synced)
    point_id = _a_point_id(synced)

    result = _add(synced, point=point_id)
    assert result.exit_code == 0, result.output

    manifest = load_manifest(synced / ".itest" / "manifest.yaml")
    entry = next(t for t in manifest.tests if t.test_name == "test_probe_health")
    assert entry.point_id == point_id
    assert entry.path == PROBE_FILE
    assert entry.tier == "active"
    # A real, human-authored body is implemented, not a stub.
    assert entry.status == "implemented"
    # resource_group defaults to the point's target, as sync does.
    assert entry.resource_group == "aws_security_group.alb"
    # Ownership hash records the file as it stands: human-owned from birth.
    from itest.core import stubgen

    assert entry.ownership_hash == stubgen.file_hash(synced / PROBE_FILE)


def test_registered_test_joins_verify(synced: Path) -> None:
    """A passing registered test flips its point from stub to passing under an
    environment that allows its tier — exactly like a synced test would."""
    _write_probe(synced)
    _add(synced)

    # dev allows active; bind to it so the active test runs.
    (synced / ".itest" / "environments.yaml").write_text(
        "version: 1\nenvironments:\n  dev: { tiers: [static, readonly, active] }\n"
    )
    (synced / ".itest" / "environment").write_text("dev\n")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "[PASS] 0.0.0.0/0 -> aws_security_group.alb" in result.output


def test_registered_active_test_is_gated_like_a_synced_one(synced: Path) -> None:
    """The point keeps its readonly stub, so the point is not gated — but the
    added active *test* is removed from the run exactly as a synced active test
    would be. That test-level gating is the precise claim."""
    import json

    _write_probe(synced)
    _add(synced)

    (synced / ".itest" / "environments.yaml").write_text(
        "version: 1\nenvironments:\n"
        "  dev: { tiers: [static, readonly, active] }\n"
        "  prod: { tiers: [static, readonly], production: true }\n"
    )

    # dev allows active: the test runs (its body passes).
    (synced / ".itest" / "environment").write_text("dev\n")
    dev = json.loads(runner.invoke(app, ["verify", "--output", "json"]).output)
    dev_probe = next(
        t for t in dev["tests"] if t["canonical"].endswith("::test_probe_health")
    )
    assert dev_probe["outcome"] == "passed"

    # prod forbids active: the test is gated out of the run, not skipped at
    # runtime — the same outcome a synced active test would get.
    (synced / ".itest" / "environment").write_text("prod\n")
    prod = json.loads(runner.invoke(app, ["verify", "--output", "json"]).output)
    prod_probe = next(
        t for t in prod["tests"] if t["canonical"].endswith("::test_probe_health")
    )
    assert prod_probe["outcome"] == "gated"


# --------------------------------------------------------------------------
# Validation: every failure is a clean, hard error (exit 2)
# --------------------------------------------------------------------------


def test_unknown_point_is_refused(synced: Path) -> None:
    _write_probe(synced)
    result = _add(synced, point="nope-not-a-point")
    assert result.exit_code == 2, result.output
    assert "nope-not-a-point" in result.output


def test_missing_file_is_refused(synced: Path) -> None:
    result = _add(synced, file="itest_tests/does_not_exist.py")
    assert result.exit_code == 2, result.output
    assert "does_not_exist.py" in result.output


def test_function_not_defined_is_refused(synced: Path) -> None:
    """AST check, not import: a name that merely appears as text is not a def."""
    (synced / "itest_tests").mkdir(parents=True, exist_ok=True)
    (synced / PROBE_FILE).write_text("# test_probe_health lives here one day\nx = 1\n")
    result = _add(synced)
    assert result.exit_code == 2, result.output
    assert "test_probe_health" in result.output


def test_duplicate_registration_is_refused(synced: Path) -> None:
    _write_probe(synced)
    first = _add(synced)
    assert first.exit_code == 0, first.output
    second = _add(synced)
    assert second.exit_code == 2, second.output
    assert "already registered" in second.output


def test_invalid_tier_is_refused(synced: Path) -> None:
    _write_probe(synced)
    result = _add(synced, tier="turbo")
    assert result.exit_code == 2, result.output
    assert "turbo" in result.output


def test_tier_is_required(synced: Path) -> None:
    _write_probe(synced)
    result = runner.invoke(
        app,
        [
            "add",
            "--point",
            _a_point_id(synced),
            "--file",
            PROBE_FILE,
            "--function",
            "test_probe_health",
        ],
    )
    # typer rejects a missing required option before the command body runs.
    assert result.exit_code != 0


# --------------------------------------------------------------------------
# Sync round-trip: human-owned from birth
# --------------------------------------------------------------------------


def test_sync_never_rewrites_relocates_or_deregisters_an_added_test(
    synced: Path,
) -> None:
    _write_probe(synced)
    _add(synced)

    before_file = (synced / PROBE_FILE).read_text()
    manifest = load_manifest(synced / ".itest" / "manifest.yaml")
    before = next(t for t in manifest.tests if t.test_name == "test_probe_health")

    # Re-run sync against the same plan.
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output

    after_manifest = load_manifest(synced / ".itest" / "manifest.yaml")
    after = next(
        (t for t in after_manifest.tests if t.test_name == "test_probe_health"),
        None,
    )
    # Still registered (not de-registered), same point and path (not relocated),
    # same tier and status, and the file body untouched (not rewritten).
    assert after is not None
    assert after.point_id == before.point_id
    assert after.path == before.path
    assert after.tier == before.tier
    assert after.status == "implemented"
    assert after.status != "orphaned"
    assert (synced / PROBE_FILE).read_text() == before_file
