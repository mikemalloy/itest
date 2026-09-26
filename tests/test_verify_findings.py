"""`itest verify` prints findings, never pytest internals.

A failing check used to print pytest's whole failure representation — the
parametrized EngineCase repr, the McpTarget repr, ``record_property``, the
test source and ``assert 'critical' == 'pass'`` — so the tool looked crashed
and the one line that mattered was buried. Now the summary line comes first,
unchanged, then a ``Findings (n):`` block: one entry per failed check in
verdict order (critical, then failing, then errored) — status word, source ->
target, the trait's title, and the detail on its own line. The detail is the
same sentence the readiness page's Answer prints for that finding, from one
function, and carries no state hash, no "sentinel" and no test node id. The
complete pytest output goes to ``.itest/verify.log`` (overwritten per run),
and ``--verbose`` prints it to the terminal as before. Exit code unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_answer import JARGON_PATTERNS, JARGON_WORDS
from typer.testing import CliRunner

from itest.cli import app
from itest.core import findings, stubgen
from itest.core.manifest import (
    IntegrationPoint,
    TestEntry,
    load_manifest,
    save_manifest,
)
from itest.report import model as report_model

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
STUB_FILE = "itest_tests/test_sg_edges.py"
SKIP_LINE = 'pytest.skip("stub: implement this integration test")'
LOG = ".itest/verify.log"

#: The critical detail the observed mutation check recorded on the 2026-09-25
#: staging page, hashes and all.
OBSERVED_DETAIL = (
    "'lookalike_read' claims read (annotation readOnlyHint) but changed "
    "observable state: 'search_records': 2 -> 3 values, 2 path(s) changed "
    "(ids[0], total) (before 50389d988895, after aba90f872549). One sentinel "
    "call did this (it answered with a tool error)"
)
OBSERVED_SENTENCE = (
    "'lookalike_read' claims read (read-only annotation) but changed "
    "observable state: 'search_records': 2 -> 3 values, 2 path(s) changed "
    "(ids[0], total)."
)

#: What must never reach the terminal on a failing run.
PYTEST_INTERNALS = ("assert ", "record_property", "EngineCase(", "McpTarget(", ".py:")


def _set_body(base_dir: Path, func: str, statement: str) -> None:
    path = base_dir / STUB_FILE
    text = path.read_text(encoding="utf-8")
    i = text.index(f"def {func}():")
    j = text.index(SKIP_LINE, i)
    end = text.index("\n", j) + 1
    path.write_text(text[:j] + statement + "\n" + text[end:], encoding="utf-8")


def _add_tool_check(project: Path, detail: str) -> None:
    """A declared tool's engine check that records a critical CheckResult —
    the shape verify's ledger reads — without a live server."""
    path = project / "itest_tests" / "tools_reference_mcp" / "test_engine.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import pytest\n\n\n"
        '@pytest.mark.parametrize("case", '
        '["lookalike_read-blast.mutation_class_observed"])\n'
        "def test_engine(case, record_property):\n"
        f"    detail = {detail!r}\n"
        '    record_property("itest_check", {"status": "critical", '
        '"detail": detail, "evidence": None, "reason": None})\n'
        '    assert "critical" == "pass", detail\n',
        encoding="utf-8",
    )
    manifest_file = project / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    seen = manifest.generated_at
    manifest.points.append(
        IntegrationPoint(
            id="90bb8861b1f7",
            type="mcp_tool",
            source="reference-mcp",
            target="lookalike_read",
            attributes={"mutation": "read", "mutation_source": "detected"},
            traits_planned=["blast.mutation_class_observed"],
            hcl_address=".itest/tools/reference-mcp.yaml",
            origin="declared",
            first_seen=seen,
            last_seen=seen,
        )
    )
    manifest.tests.append(
        TestEntry(
            id="t-observed",
            point_id="90bb8861b1f7",
            path="itest_tests/tools_reference_mcp/test_engine.py",
            test_name="test_engine[lookalike_read-blast.mutation_class_observed]",
            trait="blast.mutation_class_observed",
            ownership_hash=stubgen.file_hash(path),
            status="implemented",
            tier="readonly",
        )
    )
    save_manifest(manifest, manifest_file)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One critical tool check, one failing point, one errored point."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    _set_body(
        tmp_path, "test_sg_alb_to_web_80", "assert False, 'the guard did not hold'"
    )
    broken = tmp_path / "itest_tests" / "test_broken.py"
    broken.write_text(
        "import itest_missing_dep_xyz  # noqa: F401\n\n\ndef test_broken():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    manifest = load_manifest(tmp_path / ".itest" / "manifest.yaml")
    web_to_db = next(p for p in manifest.points if p.target.endswith(".db"))
    result = runner.invoke(
        app,
        [
            "add",
            "--point",
            web_to_db.id,
            "--file",
            "itest_tests/test_broken.py",
            "--function",
            "test_broken",
            "--tier",
            "readonly",
        ],
    )
    assert result.exit_code == 0, result.output
    _add_tool_check(tmp_path, OBSERVED_DETAIL)
    return tmp_path


def _lines(output: str) -> list[str]:
    return output.splitlines()


# --- the block ----------------------------------------------------------------------


def test_the_findings_block_replaces_the_tracebacks(project: Path) -> None:
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2, result.output  # an errored point: unchanged
    lines = _lines(result.output)
    assert lines[0].startswith("4 integration points: ")
    start = lines.index("Findings (3):")
    block = lines[start : start + 7]
    width = len("aws_security_group.alb -> aws_security_group.web")

    def row(status: str, edge: str, title: str) -> str:
        return f"  {status:<8}  {edge:<{width}}  {title}"

    assert block == [
        "Findings (3):",
        row("CRITICAL", "reference-mcp -> lookalike_read", "mutation class (observed)"),
        f"            {OBSERVED_SENTENCE}",
        row(
            "FAIL", "aws_security_group.alb -> aws_security_group.web", "tcp:80 ingress"
        ),
        "            the guard did not hold",
        row(
            "ERROR",
            "aws_security_group.web -> aws_security_group.db",
            "tcp:5432 ingress",
        ),
        "            ModuleNotFoundError: No module named 'itest_missing_dep_xyz'",
    ]
    assert f"Full test output: {LOG}" in lines
    for word in PYTEST_INTERNALS:
        assert word not in result.output, word
    assert "Failing tests:" not in result.output
    assert "Errored tests" not in result.output


def test_the_full_pytest_output_goes_to_the_log(project: Path) -> None:
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2
    log = (project / LOG).read_text(encoding="utf-8")
    assert "Failing tests:" in log and "Errored tests (the suite could not run):" in log
    assert "record_property" in log and "assert 'critical' == 'pass'" in log
    assert "itest_missing_dep_xyz" in log
    # Overwritten per run, never appended.
    runner.invoke(app, ["verify"])
    assert (project / LOG).read_text(encoding="utf-8").count("Failing tests:") == 1


def test_verbose_prints_the_full_output_to_the_terminal(project: Path) -> None:
    result = runner.invoke(app, ["verify", "--verbose"])
    assert result.exit_code == 2
    assert "Failing tests:" in result.output
    assert "record_property" in result.output
    assert _lines(result.output)[0].startswith("4 integration points: ")


def test_every_finding_sentence_is_plain(project: Path) -> None:
    """The sentences reuse the Answer's jargon contract, extended with the
    words a pytest failure would bring: sentinel, traceback, assert, and a
    twelve-hex state hash."""
    result = runner.invoke(app, ["verify"])
    lines = _lines(result.output)
    start = lines.index("Findings (3):")
    sentences = [line.strip() for line in lines[start + 1 :] if line.startswith("   ")]
    assert len(sentences) == 3
    for text in sentences:
        lowered = text.lower()
        for word in JARGON_WORDS:
            assert word not in lowered, (word, text)
        for pattern in JARGON_PATTERNS:
            assert not re.search(pattern, text), (pattern, text)


def test_exit_code_is_unchanged_without_an_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    _set_body(tmp_path, "test_sg_alb_to_web_80", "assert False, 'boom'")
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1
    assert "Findings (1):" in result.output
    assert "            boom" in result.output
    assert "assert" not in result.output


def test_a_green_run_prints_no_findings_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0
    assert "Findings" not in result.output
    assert LOG not in result.output
    assert (tmp_path / LOG).exists()


# --- one source of truth ------------------------------------------------------------


def test_the_answer_prints_the_same_sentence_as_the_cli(project: Path) -> None:
    result = runner.invoke(app, ["verify", "--output", "json"])
    document = json.loads(result.output)
    page = report_model.build(
        document, load_manifest(project / ".itest" / "manifest.yaml")
    )
    by_severity = {f.severity: f for f in page.answer.findings}
    assert list(by_severity) == ["critical", "failing", "error"]
    critical = by_severity["critical"]
    assert (critical.source, critical.target) == ("reference-mcp", "lookalike_read")
    assert critical.check == "mutation class (observed)"
    assert critical.detail == OBSERVED_SENTENCE
    assert by_severity["failing"].detail == "the guard did not hold"
    assert by_severity["failing"].check == "tcp:80 ingress"
    assert by_severity["error"].detail == (
        "ModuleNotFoundError: No module named 'itest_missing_dep_xyz'"
    )


@pytest.mark.parametrize(
    ("detail", "sentence"),
    [
        (OBSERVED_DETAIL, OBSERVED_SENTENCE),
        (
            "anonymous call succeeded on a read tool ('get_guide'): data exposure, "
            "not mutation",
            "anonymous call succeeded on a read tool ('get_guide'): data exposure, "
            "not mutation",
        ),
        (
            "anonymous call reached the read tool 'fetch_record' and it answered "
            "with a tool error (Error executing tool fetch_record: 404 record "
            "'sentinel-cannot-exist-0000' not found): the tool ran for an "
            "anonymous caller",
            "anonymous call reached the read tool 'fetch_record' and it answered "
            "with a tool error: the tool ran for an anonymous caller",
        ),
        (
            "'lookalike_read' claims read (annotation readOnlyHint) but changed "
            "observable state: 'search_records': 5 -> 6 values, 2 path(s) changed "
            "(ids[3], total) (before 7c7501203156, after ff4d80f3a3b2). One "
            "sentinel call did this (it answered with a tool error)",
            "'lookalike_read' claims read (read-only annotation) but changed "
            "observable state: 'search_records': 5 -> 6 values, 2 path(s) changed "
            "(ids[3], total).",
        ),
    ],
)
def test_plain_detail_drops_hashes_sentinels_and_quoted_tool_errors(
    detail: str, sentence: str
) -> None:
    assert findings.plain_detail(detail) == sentence
    lowered = sentence.lower()
    for word in JARGON_WORDS:
        assert word not in lowered, word
    for pattern in JARGON_PATTERNS:
        assert not re.search(pattern, sentence), pattern


def test_failure_message_is_the_one_line_that_matters() -> None:
    traceback = (
        "    def test_x():\n>       assert False, 'the guard did not hold'\n"
        "E       AssertionError: the guard did not hold\nE       assert False\n\n"
        "itest_tests/test_sg_edges.py:12: AssertionError"
    )
    assert findings.failure_message(traceback) == "the guard did not hold"
    bare = "    def test_x():\n>       assert 0 == 1\nE       assert 0 == 1\n"
    assert findings.failure_message(bare) == "the test failed"
    collection = (
        "ImportError while importing test module 'x/test_broken.py'.\n"
        "Traceback:\nE   ModuleNotFoundError: No module named 'itest_missing_dep_xyz'"
    )
    assert findings.failure_message(collection) == (
        "ModuleNotFoundError: No module named 'itest_missing_dep_xyz'"
    )
    assert findings.failure_message("") == "the test failed"


def test_the_jargon_list_covers_what_a_traceback_brings() -> None:
    for word in ("sentinel", "traceback", "assert"):
        assert word in JARGON_WORDS
    assert any(re.search(p, "before 50389d988895") for p in JARGON_PATTERNS)
    assert not any(re.search(p, "2 -> 3 values") for p in JARGON_PATTERNS)


def test_findings_are_read_from_verify_json_alone() -> None:
    """No manifest: the tool points are the ledger's own, so a point that is a
    declared tool is never listed twice."""
    document = {
        "points": [
            {
                "id": "t1",
                "status": "failing",
                "source": "s",
                "target": "tool",
                "tag": "read",
            },
            {
                "id": "p1",
                "status": "failing",
                "source": "a",
                "target": "b",
                "tag": "tcp:80",
            },
        ],
        "tests": [
            {
                "canonical": "x.py::t",
                "outcome": "failed",
                "point_id": "p1",
                "detail": "E  AssertionError: no\n",
            },
        ],
        "tools": {
            "servers": [
                {
                    "server": "s",
                    "summary": {"declared": 1, "live": 1},
                    "tools": [
                        {
                            "name": "tool",
                            "point_id": "t1",
                            "mutation": "read",
                            "mutation_source": "detected",
                            "checks": [
                                {
                                    "trait": "authority.anonymous",
                                    "status": "fail",
                                    "detail": "anonymous call succeeded",
                                    "test": "x",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    }
    found = findings.findings_for(document, findings.trait_names())
    assert [(f.severity, f.source, f.target, f.check, f.detail) for f in found] == [
        ("failing", "s", "tool", "refuses anonymous", "anonymous call succeeded"),
        ("failing", "a", "b", "tcp:80", "no"),
    ]
