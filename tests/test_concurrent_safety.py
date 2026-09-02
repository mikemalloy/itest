"""F5 regression: concurrent verify runs must not corrupt each other.

Two hazards: a fixed ``.itest/_verify_report.json`` two runs would read/write at
once, and a non-atomic ``save_manifest`` that a crash (or a racing reader) could
catch mid-write. This pins a per-run report file and an atomic manifest write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from itest.core.manifest import Manifest, load_manifest, save_manifest


def _manifest() -> Manifest:
    return Manifest(generated_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_report_files_are_per_run_and_distinct(tmp_path: Path) -> None:
    from itest.core.verifier import _new_report_file

    a = _new_report_file(tmp_path)
    b = _new_report_file(tmp_path)
    assert a != b, "two runs must not share one report file"
    assert a.exists() and b.exists()


def test_save_manifest_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / ".itest" / "manifest.yaml"
    save_manifest(_manifest(), path)
    # A completed write leaves only the manifest, no temp files beside it.
    assert [p.name for p in path.parent.iterdir()] == ["manifest.yaml"]
    assert load_manifest(path).schema_version == 2


def test_failed_write_leaves_the_original_intact(tmp_path, monkeypatch) -> None:
    from itest.core import manifest as manifest_mod

    path = tmp_path / ".itest" / "manifest.yaml"
    save_manifest(_manifest(), path)
    good = path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("disk full mid-write")

    monkeypatch.setattr(manifest_mod.yaml, "safe_dump", boom)
    with pytest.raises(RuntimeError):
        save_manifest(_manifest(), path)

    # The original is untouched (not truncated), and no temp file is left behind.
    assert path.read_text(encoding="utf-8") == good
    assert [p.name for p in path.parent.iterdir()] == ["manifest.yaml"]
