"""F2 regression: a disabled manifest entry must never be collected.

Disabled entries were skipped in the point rollup but still collected and run,
so a test disabled *because it mutates* would execute anyway. This pins that a
disabled test's module is never even imported, while the point's reporting is
unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"


@pytest.fixture
def synced_project(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    return tmp_path


def test_disabled_test_is_never_collected(synced_project: Path) -> None:
    marker = synced_project / "DISABLED_IMPORTED.marker"
    extra = synced_project / "itest_tests" / "test_disabled_probe.py"
    # An import-time side effect: if the module is collected at all, the marker
    # is written. Its absence proves the file was never even imported.
    extra.write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('imported')\n\n\n"
        "def test_disabled_probe():\n"
        "    assert True\n"
    )

    from itest.core.manifest import TestEntry, load_manifest, save_manifest

    manifest_path = synced_project / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_path)
    point_id = next(
        t.point_id
        for t in manifest.tests
        if t.test_name == "test_sg_internet_to_alb_443"
    )
    manifest.tests.append(
        TestEntry(
            id="t-disabled-probe",
            point_id=point_id,
            path="itest_tests/test_disabled_probe.py",
            test_name="test_disabled_probe",
            ownership_hash="0" * 64,
            status="implemented",
            disabled=True,
            disabled_reason="mutates; parked",
        )
    )
    save_manifest(manifest, manifest_path)

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output

    # The disabled test's module was never imported.
    assert not marker.exists()

    # Reporting is unchanged: the point still rolls up from its own stub, and
    # all three points are present.
    assert "3 integration points" in result.output
    assert "[STUB] 0.0.0.0/0 -> aws_security_group.alb" in result.output


def test_disabled_test_sharing_a_file_is_deselected_siblings_run(
    synced_project: Path,
) -> None:
    """A disabled test among enabled siblings is deselected — the module still
    imports for the siblings, but the disabled test never runs."""
    ran = synced_project / "DISABLED_BODY_RAN.marker"
    extra = synced_project / "itest_tests" / "test_mixed_probe.py"
    extra.write_text(
        "import pathlib\n\n\n"
        "def test_enabled_sibling():\n"
        "    assert True\n\n\n"
        "def test_disabled_sibling():\n"
        f"    pathlib.Path({str(ran)!r}).write_text('ran')\n"
        "    assert True\n"
    )

    from itest.core.manifest import TestEntry, load_manifest, save_manifest

    manifest_path = synced_project / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_path)
    point_id = next(
        t.point_id
        for t in manifest.tests
        if t.test_name == "test_sg_internet_to_alb_443"
    )
    siblings = (("test_enabled_sibling", False), ("test_disabled_sibling", True))
    for name, disabled in siblings:
        manifest.tests.append(
            TestEntry(
                id=f"t-{name}",
                point_id=point_id,
                path="itest_tests/test_mixed_probe.py",
                test_name=name,
                ownership_hash="0" * 64,
                status="implemented",
                disabled=disabled,
            )
        )
    save_manifest(manifest, manifest_path)

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    # The disabled sibling's body never ran; the enabled sibling made the point pass.
    assert not ran.exists()
    assert "[PASS] 0.0.0.0/0 -> aws_security_group.alb" in result.output
