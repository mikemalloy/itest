"""Every human path of every command goes through the one styled echo.

Task 1 built the colorizer and proved the invariant on canonical text. These
tests prove the commands actually use it, and — the half that matters more —
that nothing machine-read does: `--output json`, the JUnit note, the redact
payload and `--version` still go out as plain bytes.

Colour is forced through `ITEST_FORCE_COLOR` (the documented escape hatch)
together with click's `color=True`, which stops the test runner's own
non-terminal stream from stripping the escapes before the assertion sees
them. Every styled case is paired with an unstyled run of the same command,
and the pair must differ by ANSI codes and nothing else.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core import style

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ECS = FIXTURES / "aws-samples" / "ecs-fargate-alb.json"
ALEX_S6 = FIXTURES / "alex" / "alex-s6.json"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
FORCED = {style.FORCE_COLOR_ENV: "1"}


#: `verify` reports its own wall clock, which differs between two runs of the
#: same command. Neither run styles it, so folding it away compares content.
ELAPSED = re.compile(r"^Ran (\d+) tests in [\d.]+s$", re.MULTILINE)


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def normalize(text: str) -> str:
    return ELAPSED.sub(r"Ran \1 tests in <elapsed>", strip_ansi(text))


def _invoke(args: list[str], *, forced: bool = False, **kwargs):
    """One CLI run, optionally believing it is writing to a terminal."""
    if forced:
        kwargs.setdefault("env", {}).update(FORCED)
        kwargs["color"] = True
    result = runner.invoke(app, args, **kwargs)
    style.configure()  # leave the module as we found it
    return result


def _both(
    args: list[str], *, prepare: Callable[[], None] | None = None, **kwargs
) -> tuple[str, str]:
    """Return ``(plain, styled)`` output for the same command.

    ``prepare`` runs before each invocation, for the commands that write to
    the working directory and would otherwise see a different world the
    second time.
    """
    if prepare is not None:
        prepare()
    plain = _invoke(args, **kwargs)
    if prepare is not None:
        prepare()
    styled = _invoke(args, forced=True, **kwargs)
    assert plain.exit_code == styled.exit_code, (plain.output, styled.output)
    return plain.output, styled.output


def _assert_styled_pair(plain: str, styled: str) -> None:
    assert "\x1b[" not in plain, "unstyled run leaked escapes"
    assert "\x1b[" in styled, "styled run produced no escapes"
    assert normalize(styled) == normalize(plain)


def _clean(project: Path) -> Callable[[], None]:
    """Put the working directory back to before any sync ran."""

    def prepare() -> None:
        for name in (".itest", "itest_tests"):
            shutil.rmtree(project / name, ignore_errors=True)

    return prepare


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def synced(project):
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX_S6)])
    assert result.exit_code == 0, result.output
    return project


# ==========================================================================
# plan
# ==========================================================================


def test_plan_human_output_is_styled(project) -> None:
    plain, styled = _both(["plan", "--tf-json", str(ECS)])
    _assert_styled_pair(plain, styled)
    assert plain.startswith("ITest plan: 12 new")


def test_plan_json_payload_is_never_styled(project) -> None:
    result = _invoke(["plan", "--output", "json", "--tf-json", str(ECS)], forced=True)
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
    assert len(json.loads(result.output)["new_points"]) == 12


def test_plan_input_error_is_red_on_stderr(project) -> None:
    result = _invoke(["plan", "--tf-json", "nope.json"], forced=True)
    assert result.exit_code == 1
    assert "\x1b[31m" in result.stderr
    assert strip_ansi(result.stderr) == "--tf-json file not found: nope.json\n"


# ==========================================================================
# sync, including the confirmation path
# ==========================================================================


def test_sync_output_is_styled(project) -> None:
    plain, styled = _both(
        ["sync", "--auto-approve", "--tf-json", str(ALEX_S6)],
        prepare=_clean(project),
    )
    _assert_styled_pair(plain, styled)
    assert "Applied: added 14 stub(s)" in plain


def test_sync_noop_rollup_is_styled(synced) -> None:
    plain, styled = _both(["sync", "--auto-approve", "--tf-json", str(ALEX_S6)])
    _assert_styled_pair(plain, styled)
    assert "No changes to apply. Manifest is up to date." in plain


def test_sync_confirmation_path_is_wired(project) -> None:
    """Declining still prints the changeset and the cancel line, both styled."""
    plain, styled = _both(
        ["sync", "--tf-json", str(ALEX_S6)], input="n\n", prepare=_clean(project)
    )
    _assert_styled_pair(plain, styled)
    assert "Apply cancelled." in plain
    assert "ITest plan: 14 new" in plain


def test_sync_confirmation_is_byte_identical_without_a_terminal(project) -> None:
    result = runner.invoke(app, ["sync", "--tf-json", str(ALEX_S6)], input="n\n")
    assert result.exit_code == 1
    assert "\x1b[" not in result.output
    assert "Apply these changes? [y/N]: n" in result.output


def test_sync_prompt_text_is_styled_when_enabled(monkeypatch) -> None:
    """click strips escapes from a prompt on a non-terminal, so assert here.

    The prompt is handed to `typer.confirm` already decorated; on a real
    terminal click passes it through untouched.
    """
    monkeypatch.setenv(style.FORCE_COLOR_ENV, "1")
    style.configure()
    try:
        decorated = style.decorate("Apply these changes?")
        assert decorated != "Apply these changes?"
        assert strip_ansi(decorated) == "Apply these changes?"
    finally:
        monkeypatch.delenv(style.FORCE_COLOR_ENV, raising=False)
        style.configure()


# ==========================================================================
# verify
# ==========================================================================


def test_verify_human_output_is_styled(synced) -> None:
    plain, styled = _both(["verify"])
    _assert_styled_pair(plain, styled)
    assert "14 integration points" in plain
    assert plain.count("[STUB]") == 14


def test_verify_json_payload_is_never_styled(synced) -> None:
    result = _invoke(["verify", "--output", "json"], forced=True)
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
    assert json.loads(result.output)["total_points"] == 14


def test_verify_junit_note_is_plain_and_its_rollup_is_styled(synced) -> None:
    plain, styled = _both(["verify", "--output", "junit"])
    _assert_styled_pair(plain, styled)

    note, rollup = styled.splitlines()[:2]
    assert note == "Wrote JUnit XML to itest-results.xml", "the note must stay plain"
    assert "\x1b[" in rollup


def test_verify_styles_after_redaction_never_before(synced) -> None:
    """The account id must be gone before a single escape is added."""
    from itest.core.manifest import load_manifest, save_manifest

    fake = "999988887777"
    manifest_path = synced / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_path)
    manifest.points[0].target = f"arn:aws:sqs:us-west-1:{fake}:private-queue"
    save_manifest(manifest, manifest_path)

    plain, styled = _both(["verify", "--redact"])
    _assert_styled_pair(plain, styled)
    assert fake not in styled
    assert "111111111111" in strip_ansi(styled)
    assert fake not in plain


def test_verify_exit_code_survives_styling(synced) -> None:
    """Styling is presentation; a failing run still exits 1."""
    stub_file = synced / "itest_tests" / "test_iam_edges.py"
    text = stub_file.read_text(encoding="utf-8")
    stub_file.write_text(
        text.replace(
            'pytest.skip("stub: implement this integration test")',
            "assert False, 'boom'",
            1,
        ),
        encoding="utf-8",
    )

    plain = _invoke(["verify"])
    styled = _invoke(["verify"], forced=True)
    assert plain.exit_code == 1
    assert styled.exit_code == 1
    _assert_styled_pair(plain.output, styled.output)
    assert "\x1b[" in styled.output


# ==========================================================================
# redact
# ==========================================================================


SENSITIVE = {
    "values": {
        "root_module": {
            "resources": [
                {
                    "address": "aws_db_instance.db",
                    "mode": "managed",
                    "type": "aws_db_instance",
                    "name": "db",
                    "values": {"password": "hunter2", "id": "x"},
                    "sensitive_values": {"password": True},
                }
            ]
        }
    }
}


@pytest.fixture
def dirty(project) -> Path:
    path = project / "dirty.json"
    path.write_text(json.dumps(SENSITIVE), encoding="utf-8")
    return path


def test_redact_check_findings_are_styled(dirty: Path) -> None:
    plain, styled = _both(["redact", "--check", str(dirty)])
    _assert_styled_pair(plain, styled)
    assert plain.startswith("1 finding(s)")


def test_redact_clean_summary_is_styled(project) -> None:
    path = project / "clean.json"
    path.write_text(json.dumps({"values": {"root_module": {}}}), encoding="utf-8")
    plain, styled = _both(["redact", "--check", str(path)])
    _assert_styled_pair(plain, styled)
    assert plain.strip() == "No secrets found. Safe to share."


def test_redact_check_json_is_never_styled(dirty: Path) -> None:
    result = _invoke(["redact", "--check", "--output", "json", str(dirty)], forced=True)
    assert result.exit_code == 1
    assert "\x1b[" not in result.output
    assert json.loads(result.output)["finding_count"] == 1


def test_redact_payload_is_never_styled(dirty: Path) -> None:
    """The sanitized document is data; an escape in it would corrupt the file."""
    result = _invoke(["redact", str(dirty)], forced=True)
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
    assert json.loads(result.output)["values"]["root_module"]["resources"]


def test_redact_input_error_is_red_on_stderr(project) -> None:
    result = _invoke(["redact", "missing.json"], forced=True)
    assert result.exit_code == 2
    assert "\x1b[31m" in result.stderr
    assert strip_ansi(result.stderr) == "Input file not found: missing.json\n"


# ==========================================================================
# The regression guard: nothing changed for anyone without a terminal
# ==========================================================================


@pytest.mark.parametrize(
    "args",
    [
        ["plan", "--tf-json", str(ECS)],
        ["plan", "--tf-json", str(ALEX_S6)],
        ["sync", "--auto-approve", "--tf-json", str(ALEX_S6)],
    ],
)
def test_no_escapes_without_a_terminal(args: list[str], project) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output


def test_no_color_env_beats_forced_color(project) -> None:
    result = _invoke(
        ["plan", "--tf-json", str(ALEX_S6)],
        forced=True,
        env={"NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output


def test_no_color_flag_beats_forced_color(project) -> None:
    result = _invoke(["--no-color", "plan", "--tf-json", str(ALEX_S6)], forced=True)
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output


def test_version_stays_plain() -> None:
    result = _invoke(["--version"], forced=True)
    assert result.exit_code == 0
    assert result.output.strip() == "0.2.0"
