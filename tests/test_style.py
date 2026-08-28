"""The style layer is a colorizer over the canonical strings, not a rewrite.

The whole contract is one invariant: **styled output with ANSI codes stripped
is byte-for-byte the plain output**. Every render function
(`planner.render_changeset`, `verifier.render_human`,
`redact.render_findings`, `cli.render_verify_line`) stays the single source of
what ITest says; `itest.core.style` only decides which spans of that text are
bold, dim, or coloured.

That is why these tests never assert on a re-laid-out table: they take real
canonical text from the committed fixtures, run it through the layer, and
check that nothing moved. A wrapped, padded, or truncated line would be a
content change, so the long module-nested lines of the ecs-fargate-alb plan
are the interesting case — Rich wraps at width 80 by default, and this layer
must not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core import planner, style
from itest.core.detectors.base import detect_all
from itest.core.verifier import PointResult, VerifyReport, render_human

# Aliased: pytest tries to collect any imported class whose name starts with
# `Test`, and warns when it cannot.
from itest.core.verifier import TestResult as VerifiedTest

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ECS = FIXTURES / "aws-samples" / "ecs-fargate-alb.json"
ALEX_S6 = FIXTURES / "alex" / "alex-s6.json"

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def _plan_text(path: Path) -> str:
    """The canonical changeset render for a fixture, with no manifest."""
    document = json.loads(path.read_text(encoding="utf-8"))
    points, unanalyzed = detect_all(document)
    changeset = planner.compute_changeset(points, unanalyzed, set(), [])
    return planner.render_changeset(changeset)


def _styles_over(text_obj, needle: str) -> set[str]:
    """Every style name covering the first occurrence of ``needle``."""
    start = text_obj.plain.index(needle)
    end = start + len(needle)
    return {
        str(span.style)
        for span in text_obj.spans
        if span.start <= start and span.end >= end
    }


@pytest.fixture
def forced(monkeypatch):
    """A run in which the layer believes it is writing to a terminal."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv(style.FORCE_COLOR_ENV, "1")
    style.configure()
    yield
    monkeypatch.delenv(style.FORCE_COLOR_ENV, raising=False)
    style.configure()


# ==========================================================================
# 1. The invariant: strip the ANSI and you have the canonical text back
# ==========================================================================


@pytest.mark.parametrize("name", ["ecs-fargate-alb", "alex-s6"])
def test_strip_ansi_equals_plain_for_plan_output(name: str) -> None:
    plain = _plan_text(ECS if name == "ecs-fargate-alb" else ALEX_S6)
    assert strip_ansi(style.render_ansi(plain)) == plain


@pytest.mark.parametrize("fixture", [ECS, ALEX_S6], ids=["ecs-fargate-alb", "alex-s6"])
def test_strip_ansi_equals_plain_for_verify_output(
    fixture: Path, tmp_path, monkeypatch
) -> None:
    """Real verify output, produced by really running the suite."""
    monkeypatch.chdir(tmp_path)
    synced = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(fixture)])
    assert synced.exit_code == 0, synced.output
    verified = runner.invoke(app, ["verify"])
    assert verified.exit_code == 0, verified.output

    plain = verified.output
    assert "integration points:" in plain
    assert strip_ansi(style.render_ansi(plain)) == plain


def test_long_module_nested_lines_are_not_wrapped() -> None:
    """Rich wraps at 80 on a non-terminal console; a wrapped line is content."""
    plain = _plan_text(ECS)
    longest = max(plain.splitlines(), key=len)
    assert len(longest) > 120, "fixture no longer exercises the wrapping case"

    styled = strip_ansi(style.render_ansi(plain))
    assert longest in styled.splitlines()
    assert len(styled.splitlines()) == len(plain.splitlines())


def test_trailing_and_blank_lines_survive() -> None:
    plain = "header  \n\n  padded   \n\n"
    assert strip_ansi(style.render_ansi(plain)) == plain


# ==========================================================================
# 2. Gating: TTY, NO_COLOR, --no-color
# ==========================================================================


def test_decorate_is_identity_without_a_terminal() -> None:
    """Under pytest stdout is captured, so this is the real non-TTY path."""
    style.configure()
    plain = _plan_text(ECS)
    assert style.decorate(plain) == plain
    assert not style.enabled()


def test_decorate_styles_on_a_terminal(forced) -> None:
    plain = _plan_text(ECS)
    assert style.enabled()
    decorated = style.decorate(plain)
    assert decorated != plain
    assert strip_ansi(decorated) == plain


@pytest.mark.parametrize("value", ["1", "", "anything"])
def test_no_color_env_disables_even_on_a_terminal(value: str, monkeypatch) -> None:
    """Any value, per the task: presence of the variable is the signal."""
    monkeypatch.setenv(style.FORCE_COLOR_ENV, "1")
    monkeypatch.setenv("NO_COLOR", value)
    style.configure()
    plain = _plan_text(ALEX_S6)
    assert not style.enabled()
    assert style.decorate(plain) == plain


def test_no_color_option_disables_even_on_a_terminal(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv(style.FORCE_COLOR_ENV, "1")
    style.configure(no_color=True)
    try:
        assert not style.enabled()
        assert style.decorate("ITest plan: 1 new") == "ITest plan: 1 new"
    finally:
        style.configure()


def test_cli_plan_output_is_byte_identical_without_a_tty(tmp_path, monkeypatch) -> None:
    """The golden case: today's output, unchanged, when nothing is a terminal."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plan", "--tf-json", str(ECS)])
    assert result.exit_code == 0, result.output
    assert result.output == _plan_text(ECS) + "\n"
    assert "\x1b[" not in result.output


def test_cli_no_color_flag_is_accepted(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--no-color", "plan", "--tf-json", str(ALEX_S6)])
    assert result.exit_code == 0, result.output
    assert result.output == _plan_text(ALEX_S6) + "\n"


# ==========================================================================
# 3. Rollup lines
# ==========================================================================


def test_plan_summary_line_is_bold() -> None:
    rendered = style.render(_plan_text(ECS))
    line = _plan_text(ECS).splitlines()[0]
    assert "bold" in _styles_over(rendered, line)


def test_sync_summary_line_is_bold() -> None:
    line = (
        "Applied: added 12 stub(s), flagged 0 orphan(s), "
        "0 human-modified file(s) preserved."
    )
    assert "bold" in _styles_over(style.render(line), line)


def test_verify_rollup_is_bold_and_greens_the_passing_count() -> None:
    line = (
        "12 integration points: 12 passing, 0 failing, 0 errored, "
        "0 stubs, 0 orphaned tests."
    )
    rendered = style.render(line)
    assert "bold" in _styles_over(rendered, line)
    assert "bold green" in _styles_over(rendered, "12 passing")
    assert not _styles_over(rendered, "0 failing") - {"bold"}


def test_verify_rollup_reds_a_nonzero_failing_count() -> None:
    line = (
        "12 integration points: 9 passing, 3 failing, 0 errored, "
        "0 stubs, 0 orphaned tests."
    )
    rendered = style.render(line)
    assert "bold red" in _styles_over(rendered, "3 failing")
    # Nothing is green while something is failing.
    assert "bold green" not in _styles_over(rendered, "9 passing")


def test_verify_rollup_withholds_green_while_something_errored() -> None:
    line = (
        "12 integration points: 0 passing, 0 failing, 12 errored, "
        "0 stubs, 0 orphaned tests."
    )
    assert "bold green" not in _styles_over(style.render(line), "0 passing")


def test_short_rollup_from_the_junit_path_is_handled() -> None:
    """`cli.render_verify_line` omits the errored count; same rule applies."""
    line = "3 integration points: 3 passing, 0 failing, 0 stubs, 0 orphaned tests."
    rendered = style.render(line)
    assert "bold" in _styles_over(rendered, line)
    assert "bold green" in _styles_over(rendered, "3 passing")


# ==========================================================================
# 4. Plan changeset
# ==========================================================================


def test_new_point_marker_and_tag_are_green() -> None:
    line = "  + [tcp:443 ingress] 0.0.0.0/0 -> aws_security_group.alb"
    rendered = style.render(line)
    assert "green" in _styles_over(rendered, "+")
    assert "green" in _styles_over(rendered, "[tcp:443 ingress]")
    assert not _styles_over(rendered, "aws_security_group.alb")


def test_green_tag_spans_nested_brackets() -> None:
    """An lb_edge tag carries brackets of its own, and so do the addresses."""
    tag = (
        '[HTTP:80 -> module.alb.aws_lb_target_group.this["ex-ecs"] '
        "[priority 1 path=/*] [weight 100]]"
    )
    target = 'module.alb.aws_lb_target_group.this["ex-ecs"]'
    line = f"  + {tag} module.alb.aws_lb.this[0] -> {target}"
    rendered = style.render(line)
    assert "green" in _styles_over(rendered, tag)
    # The target after the tag keeps its own brackets and stays unstyled.
    assert not _styles_over(rendered, "module.alb.aws_lb.this[0]")


def test_orphan_lines_are_yellow() -> None:
    line = "  ~ itest_tests/test_sg_edges.py::test_sg_a_to_b  (was point 1234abcd)"
    assert "yellow" in _styles_over(style.render(line), line)


def test_resurrected_lines_are_cyan() -> None:
    line = "  ^ [returning] aws_security_group.web -> aws_security_group.db"
    assert "cyan" in _styles_over(style.render(line), line)


@pytest.mark.parametrize(
    "header",
    [
        "New integration points (12):",
        "Orphan candidates (0):",
        "Not analyzed (37 resource(s)):",
        "Resurrected (2):",
        "Points:",
        "Unregistered tests (not in manifest):",
    ],
)
def test_section_headers_are_bold(header: str) -> None:
    assert "bold" in _styles_over(style.render(header), header)


def test_detail_lines_are_dim() -> None:
    line = '      id=255245fbf7d0  hcl=module.alb.aws_lb_listener_rule.this["x"]'
    assert "dim" in _styles_over(style.render(line), line)


# ==========================================================================
# 5. Verify points
# ==========================================================================


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("PASS", "green"),
        ("FAIL", "bold red"),
        ("ERROR", "magenta"),
        ("STUB", "dim"),
        ("ORPHAN", "yellow"),
    ],
)
def test_point_status_tags(tag: str, expected: str) -> None:
    line = (
        f"  [{tag}] aws_security_group.web -> aws_security_group.db (tcp:5432 ingress)"
    )
    rendered = style.render(line)
    assert expected in _styles_over(rendered, f"[{tag}]")
    # Only the tag: the point itself is not repainted.
    assert not _styles_over(rendered, "aws_security_group.web")


def test_unknown_status_tag_is_left_alone() -> None:
    line = "  [????] a -> b (tag)"
    assert not _styles_over(style.render(line), "[????]")


def test_failing_tests_header_is_red_and_bold() -> None:
    assert _styles_over(style.render("Failing tests:"), "Failing tests:") == {
        "bold red"
    }


def test_traceback_block_is_left_unstyled() -> None:
    """Pytest already formatted it; and it may contain our own keywords."""
    plain = "\n".join(
        [
            "Failing tests:",
            "  itest_tests/test_iam_edges.py::test_iam_a_to_b",
            "      E  AssertionError: wildcard_resource DENY BROAD [open]",
            "      E  assert 0 == 1",
            "",
            "Unregistered tests (not in manifest):",
            "  itest_tests/test_x.py::test_y",
        ]
    )
    rendered = style.render(plain)
    assert not _styles_over(rendered, "wildcard_resource DENY BROAD [open]")
    assert not _styles_over(rendered, "      E  assert 0 == 1")
    # The block ends at the blank line; what follows is styled again.
    assert "bold" in _styles_over(rendered, "Unregistered tests (not in manifest):")


def test_a_report_with_failures_still_round_trips() -> None:
    report = VerifyReport(
        total_points=2,
        passing=1,
        failing=1,
        stubs=0,
        elapsed_seconds=1.25,
        points=[
            PointResult(
                id="a", source="x", target="y", status="passing", tag="tcp:80 ingress"
            ),
            PointResult(
                id="b",
                source="r",
                target="*",
                status="failing",
                tag="s3 (2 actions) [wildcard_resource, external]",
            ),
        ],
        tests=[
            VerifiedTest(
                canonical="itest_tests/test_iam_edges.py::test_b",
                outcome="failed",
                point_id="b",
                detail="E   assert False\nE   where False = allowed()",
            )
        ],
    )
    plain = render_human(report)
    assert strip_ansi(style.render_ansi(plain)) == plain


# ==========================================================================
# 6. Finding-class flags
# ==========================================================================


@pytest.mark.parametrize(
    "flag",
    ["[open]", "BROAD", "DENY", "wildcard_action", "wildcard_resource"],
)
def test_finding_flags_are_yellow(flag: str) -> None:
    line = f"  [STUB] a -> b (something {flag} trailing)"
    assert "yellow" in _styles_over(style.render(line), flag)


def test_external_is_context_not_a_finding() -> None:
    line = "  + [s3 (2 actions) [wildcard_resource, external]] role -> *"
    rendered = style.render(line)
    assert "yellow" in _styles_over(rendered, "wildcard_resource")
    assert "yellow" not in _styles_over(rendered, "external")


def test_flags_are_yellow_inside_a_green_plan_tag() -> None:
    line = "  + [managed policy BROAD] aws_iam_role.r -> arn:aws:iam::aws:policy/X"
    rendered = style.render(line)
    assert "green" in _styles_over(rendered, "[managed policy BROAD]")
    assert "yellow" in _styles_over(rendered, "BROAD")


# ==========================================================================
# 7. Errors and redact findings
# ==========================================================================


def test_errors_are_red() -> None:
    message = "--tf-json file not found: nope.json\nsecond line of the same error"
    assert "red" in _styles_over(style.render(message, error=True), message)


def test_error_rendering_still_round_trips() -> None:
    message = "`terraform show -json` failed:\nboom\nPass --tf-json PATH instead."
    assert strip_ansi(style.render_ansi(message, error=True)) == message


def test_redact_findings_headers_and_details() -> None:
    plain = "\n".join(
        [
            "3 finding(s) — this document is NOT safe to share.",
            "",
            "lambda_env (2):",
            "  values.root_module.resources[0].values.environment",
            "      DATABASE_URL",
            "",
            "Run without --check to write a sanitized copy.",
        ]
    )
    rendered = style.render(plain)
    assert "bold" in _styles_over(rendered, plain.splitlines()[0])
    assert "bold" in _styles_over(rendered, "lambda_env (2):")
    assert "dim" in _styles_over(rendered, "      DATABASE_URL")
    assert strip_ansi(style.render_ansi(plain)) == plain


def test_redact_clean_summary_is_bold() -> None:
    line = "No secrets found. Safe to share."
    assert "bold" in _styles_over(style.render(line), line)


def test_rollup_zero_passing_stubs_only_not_green() -> None:
    """A stubs-only run is clean, but "0 passing" must not render green."""
    line = (
        "12 integration points: 0 passing, 0 failing, 0 errored, "
        "12 stubs, 0 orphaned tests."
    )
    assert "bold green" not in _styles_over(style.render(line), "0 passing")
