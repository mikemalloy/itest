"""Generated stubs are routed into one file per point type.

Every stub used to land in ``test_sg_edges.py`` whatever it covered, so an IAM
grant was written into a file named for security groups. The rules that make
routing safe — per-file ownership hashes, per-file human-modified detection,
append-only writes, and never moving a test that already exists — are what
these tests pin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core.manifest import load_manifest

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
WEB = FIXTURES / "simple-web-app-plan.json"
ALEX_S5 = FIXTURES / "alex" / "alex-s5.json"
ALEX_S6 = FIXTURES / "alex" / "alex-s6.json"

SG_FILE = "itest_tests/test_sg_edges.py"
IAM_FILE = "itest_tests/test_iam_edges.py"
EVENT_FILE = "itest_tests/test_event_edges.py"

SKIP_LINE = 'pytest.skip("stub: implement this integration test")'


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _sync(source: Path):
    return runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(source)])


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resources(document: dict) -> list:
    root = document.get("planned_values") or document.get("values")
    return root["root_module"]["resources"]


def _merged(target: Path, *sources: Path) -> Path:
    """Write a document combining the resources of several fixtures."""
    merged = _load(sources[0])
    root = merged.get("planned_values") or merged.get("values")
    root["root_module"]["resources"] = [
        resource for source in sources for resource in _resources(_load(source))
    ]
    target.write_text(json.dumps(merged), encoding="utf-8")
    return target


def _stub_count(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("\ndef test_")


def _function_body(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(f"def {name}(")
    rest = text[start:]
    end = rest.find("\ndef ", 1)
    return rest if end == -1 else rest[:end]


# --------------------------------------------------------------------------
# 1. Routing
# --------------------------------------------------------------------------


def test_points_are_routed_to_a_file_per_type(workdir: Path) -> None:
    """alex-s6 is 12 IAM edges and 2 event edges, and no security groups."""
    result = _sync(ALEX_S6)
    assert result.exit_code == 0, result.output

    assert _stub_count(workdir / IAM_FILE) == 12
    assert _stub_count(workdir / EVENT_FILE) == 2
    # Nothing in this stage is a security-group edge, so no such file exists.
    assert not (workdir / SG_FILE).exists()

    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    points = {p.id: p for p in manifest.points}
    for test in manifest.tests:
        expected = f"itest_tests/test_{points[test.point_id].type}s.py"
        assert test.path == expected, f"{test.test_name} landed in {test.path}"


def test_every_generated_file_is_importable(workdir: Path) -> None:
    """A freshly generated file must parse: it only imports pytest."""
    _sync(ALEX_S6)
    for rel in (IAM_FILE, EVENT_FILE):
        source = (workdir / rel).read_text(encoding="utf-8")
        compile(source, rel, "exec")


# --------------------------------------------------------------------------
# 2. Appending across syncs
# --------------------------------------------------------------------------


def test_later_sync_appends_to_the_right_files(workdir: Path) -> None:
    """web-app first (3 sg), then alex-s5 (1 sg + 4 iam) in the same project."""
    _sync(WEB)
    sg_after_first = (workdir / SG_FILE).read_text(encoding="utf-8")
    assert _stub_count(workdir / SG_FILE) == 3

    result = _sync(ALEX_S5)
    assert result.exit_code == 0, result.output

    # The original three sg stubs are untouched, byte for byte, and the new
    # sg edge is appended after them.
    sg_after_second = (workdir / SG_FILE).read_text(encoding="utf-8")
    assert sg_after_second.startswith(sg_after_first)
    assert _stub_count(workdir / SG_FILE) == 4

    # The IAM edges went to their own file, which did not exist before.
    assert _stub_count(workdir / IAM_FILE) == 4

    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    by_path: dict[str, int] = {}
    for test in manifest.tests:
        by_path[test.path] = by_path.get(test.path, 0) + 1
    assert by_path == {SG_FILE: 4, IAM_FILE: 4}


def test_resyncing_the_same_input_is_a_noop_for_every_file(workdir: Path) -> None:
    _sync(WEB)
    _sync(ALEX_S5)
    before = {
        rel: (workdir / rel).read_text(encoding="utf-8") for rel in (SG_FILE, IAM_FILE)
    }

    result = _sync(ALEX_S5)
    assert result.exit_code == 0
    for rel, text in before.items():
        assert (workdir / rel).read_text(encoding="utf-8") == text, f"{rel} changed"


# --------------------------------------------------------------------------
# 3. Human edits, per file
# --------------------------------------------------------------------------


def test_human_edit_is_preserved_and_counted_per_file(workdir: Path) -> None:
    """A hand-edited IAM file must not make the sg file look modified too."""
    _sync(ALEX_S5)

    iam_path = workdir / IAM_FILE
    text = iam_path.read_text(encoding="utf-8")
    name = text.split("\ndef ", 1)[1].split("(")[0]
    marker = f"def {name}("
    index = text.index(marker)
    edited = (
        text[:index]
        + marker
        + '):\n    """human implemented"""\n'
        + "    assert 1 + 1 == 2  # human-written check\n"
    )
    iam_path.write_text(edited, encoding="utf-8")
    hand_written = iam_path.read_text(encoding="utf-8")

    # A document that adds both new IAM points and new sg points.
    combined = _merged(workdir / "combined.json", ALEX_S5, ALEX_S6, WEB)
    result = _sync(combined)
    assert result.exit_code == 0, result.output

    # The hand-written body survived, and new stubs were appended after it.
    assert iam_path.read_text(encoding="utf-8").startswith(hand_written)
    assert "human-written check" in iam_path.read_text(encoding="utf-8")
    # The edit truncated the file at its first function, so one stub survives
    # the human's rewrite and the 12 new IAM points are appended after it.
    assert _stub_count(iam_path) == 13

    # New sg points landed in the sg file.
    assert _stub_count(workdir / SG_FILE) == 4

    # Exactly one file was human-modified, not every file that got stubs.
    assert "1 human-modified file(s) preserved" in result.output


# --------------------------------------------------------------------------
# 4. verify still maps every test to its point
# --------------------------------------------------------------------------


def test_verify_maps_every_test_after_routing(workdir: Path) -> None:
    _sync(ALEX_S6)

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "14 integration points: 0 passing, 0 failing, 0 errored, 14 stubs" in (
        result.output
    )
    assert result.output.count("[STUB]") == 14


def test_existing_manifest_paths_are_never_moved(workdir: Path) -> None:
    """A manifest written before routing must keep working untouched.

    Sync writes sg edges to test_sg_edges.py either way, so a pre-routing
    manifest is indistinguishable from a fresh one for that type — but the
    guarantee is that sync never rewrites a recorded path.
    """
    _sync(WEB)
    manifest_path = workdir / ".itest" / "manifest.yaml"
    before = {t.test_name: t.path for t in load_manifest(manifest_path).tests}

    _sync(ALEX_S5)

    after = {t.test_name: t.path for t in load_manifest(manifest_path).tests}
    for name, path in before.items():
        assert after[name] == path, f"{name} was moved from {path} to {after[name]}"


# --------------------------------------------------------------------------
# 5. One broken module must not blind the others
# --------------------------------------------------------------------------


def test_broken_module_does_not_blind_other_files(workdir: Path) -> None:
    """A syntax error in one stub file must not error every other point.

    Regression: pytest aborts collection on the first un-importable module, so
    every point in every *other* file resolved to "missing" and rolled up as an
    error too. With one file per point type that is much worse: a typo in the
    IAM file hid the sg and event results entirely.
    """
    _sync(ALEX_S6)

    # Make one event stub genuinely pass.
    event_path = workdir / EVENT_FILE
    text = event_path.read_text(encoding="utf-8")
    index = text.index(SKIP_LINE)
    end = text.index("\n", index) + 1
    event_path.write_text(text[:index] + "assert True\n" + text[end:])

    # Break the IAM file so it cannot be imported at all.
    iam_path = workdir / IAM_FILE
    iam_path.write_text("def (\n" + iam_path.read_text(encoding="utf-8"))

    result = runner.invoke(app, ["verify"])

    # The event point still ran and passed; the IAM points are errored.
    assert "1 passing" in result.output, result.output
    assert "12 errored" in result.output, result.output
    assert result.output.count("[ERROR]") == 12
    assert "[PASS]" in result.output
    assert result.exit_code == 2, result.output


def test_verify_reports_wall_clock(workdir: Path) -> None:
    """Human output carries how long the suite actually took."""
    _sync(ALEX_S6)

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output

    line = next(line for line in result.output.splitlines() if line.startswith("Ran "))
    match = re.fullmatch(r"Ran (\d+) tests? in (\d+\.\d+)s", line)
    assert match is not None, f"unparseable wall-clock line: {line!r}"
    assert int(match.group(1)) == 14
    assert float(match.group(2)) >= 0.0


def test_verify_json_carries_elapsed_seconds(workdir: Path) -> None:
    _sync(ALEX_S6)
    result = runner.invoke(app, ["verify", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload["elapsed_seconds"], float)
    assert payload["elapsed_seconds"] >= 0.0
