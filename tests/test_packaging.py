"""The built wheel carries the data the engine reads at run time.

The trait table and the readiness template are not Python, so setuptools leaves
them out of a distribution unless it is told otherwise — and an installed
``itest`` without its trait table cannot plan a declared server at all. This
builds a real wheel, installs it into a fresh venv outside the source tree, and
asks the *installed* package for both files through ``importlib.resources``,
which is the only way the engine is allowed to find them.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

PROBE = """\
import importlib.resources as r, itest, itest.traits
assert "site-packages" in itest.__file__, itest.__file__
table = r.files("itest.traits").joinpath("traits.yaml")
template = r.files("itest.report").joinpath("templates/readiness.html")
assert table.is_file(), "traits.yaml missing from the wheel"
assert template.is_file(), "readiness.html missing from the wheel"
assert "traits:" in table.read_text(encoding="utf-8")
assert "itest:data:PAGE" in template.read_text(encoding="utf-8")
print("ok")
"""


def _build_command(out_dir: Path) -> list[str] | None:
    """``python -m build --wheel`` when available, else ``pip wheel``.

    ``--no-isolation`` builds with this interpreter's setuptools rather than
    fetching one into an isolated environment, so the test needs no network.
    """
    if importlib.util.find_spec("build") is not None:
        return [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(out_dir),
        ]
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w"] + [
            str(out_dir)
        ]
    return None


@pytest.mark.slow
def test_the_wheel_ships_the_trait_table_and_the_report_template(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel"
    command = _build_command(wheel_dir)
    if command is None:
        pytest.skip("neither `build` nor `pip` is available to build a wheel")
    built = subprocess.run(
        command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300
    )
    assert built.returncode == 0, built.stdout + built.stderr
    (wheel,) = wheel_dir.glob("itest_cli-*.whl")

    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    python = env_dir / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    # --no-deps: the probe imports only itest and itest.traits, which need
    # nothing beyond the standard library, so no index is consulted.
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "-q", str(wheel)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    # Run from outside the checkout, so `import itest` cannot find the source.
    probe = subprocess.run(
        [str(python), "-c", PROBE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.strip() == "ok"


# --- both are found through importlib.resources, never a source-relative path ---


def _fake_files(package: str, root: Path):
    """An ``importlib.resources`` stand-in that serves ``package`` from ``root``."""
    from types import SimpleNamespace

    def files(name: str):
        assert name == package, name
        return root

    return SimpleNamespace(files=files)


def test_the_trait_table_is_read_through_importlib_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from itest.core.declarations import traits as traits_module

    shipped = (REPO_ROOT / "itest" / "traits" / "traits.yaml").read_text("utf-8")
    (tmp_path / "traits.yaml").write_text(
        shipped.replace("name: refuses anonymous", "name: served by resources"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        traits_module, "resources", _fake_files("itest.traits", tmp_path)
    )
    assert (
        traits_module.load_traits().get("authority.anonymous").name
        == "served by resources"
    )


def test_the_report_template_is_read_through_importlib_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from itest.report import render as render_module

    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "readiness.html").write_text("sentinel", "utf-8")
    monkeypatch.setattr(
        render_module, "resources", _fake_files("itest.report", tmp_path)
    )
    assert render_module.template_text() == "sentinel"
