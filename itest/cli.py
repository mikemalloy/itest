from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from itest import __version__
from itest.core import style

app = typer.Typer(
    help=(
        "ITest — analyze Terraform, extract integration points, verify infrastructure."
    ),
    no_args_is_help=True,
    add_completion=False,
)


#: The one canonical string the CLI itself owns rather than a render function.
CONFIRM_PROMPT = "Apply these changes?"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


def echo(message: str, err: bool = False) -> None:
    """Print one canonical string, styled when the stream is a terminal.

    The single styled output path. When styling is off — no terminal,
    NO_COLOR, --no-color — this is byte-for-byte the `typer.echo` call it
    replaced. Paths whose bytes are consumed by a machine (json payloads, the
    JUnit note, the sanitized document, the version) keep calling
    `typer.echo` directly.

    `color=True` on the styled branch stops click from stripping the escapes
    back out again: click removes ANSI when its stream is not a terminal, and
    the one case where we style anyway is the documented ITEST_FORCE_COLOR
    hatch. On a real terminal click passes them through regardless.
    """
    if not style.enabled(err=err):
        typer.echo(message, err=err)
        return
    typer.echo(style.render_ansi(message, error=err), err=err, color=True)


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the ITest version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    no_color: bool = typer.Option(
        False,
        "--no-color",
        help="Disable colored output, as NO_COLOR in the environment does.",
    ),
) -> None:
    """ITest command-line interface."""
    # Once per invocation, so the switch and the environment are read now
    # rather than frozen at import time.
    style.configure(no_color=no_color)


@app.command()
def plan(
    # B008: typer's declarative API requires the Option() call in the default.
    # This is the documented idiom, not an accidental shared mutable default.
    tf_json: Path | None = typer.Option(  # noqa: B008
        None,
        "--tf-json",
        help="Path to a `terraform show -json` file. If omitted, runs terraform.",
    ),
    output: str = typer.Option(
        "human", "--output", help="Output format: human or json."
    ),
) -> None:
    """Detect integration points and propose a changeset."""
    from itest.core import planner

    base_dir = Path.cwd()
    try:
        changeset = planner.run_plan(tf_json, base_dir)
    except planner.PlanInputError as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    if output == "json":
        typer.echo(changeset.model_dump_json(indent=2))
    else:
        echo(planner.render_changeset(changeset))


@app.command()
def sync(
    auto_approve: bool = typer.Option(
        False, "--auto-approve", help="Apply without an interactive prompt."
    ),
    # B008: see the note on `plan` above — typer requires the call here.
    tf_json: Path | None = typer.Option(  # noqa: B008
        None,
        "--tf-json",
        help="Path to a `terraform show -json` file for the implicit plan.",
    ),
) -> None:
    """Apply the plan: update the manifest and generate test stubs."""
    from itest.core import planner, syncer

    base_dir = Path.cwd()
    try:
        changeset, note = syncer.prepare(tf_json, base_dir)
    except planner.PlanInputError as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    if note:
        echo(note)
    echo(planner.render_changeset(changeset))
    echo("")

    if syncer.is_noop(changeset):
        # Nothing to apply, but a stub implemented by hand since the last run
        # still has to be recorded: status is derived from the body, not from
        # whether the plan moved.
        reclassified = syncer.reconcile(base_dir)
        if reclassified:
            echo(f"Reclassified {reclassified} test(s) from their bodies.")
        else:
            echo("No changes to apply. Manifest is up to date.")
        return

    if not auto_approve:
        # click writes the prompt itself, so it is decorated rather than
        # echoed; on a non-terminal `decorate` hands back the same string.
        if not typer.confirm(style.decorate(CONFIRM_PROMPT)):
            echo("Apply cancelled.")
            raise typer.Exit(code=1)

    result = syncer.apply(changeset, base_dir)
    echo(result.summary())


@app.command()
def verify(
    output: str = typer.Option(
        "human", "--output", help="Output format: human, json, or junit."
    ),
    redact: bool = typer.Option(
        False,
        "--redact",
        help="Pseudonymize AWS account IDs in the output, for safe sharing.",
    ),
    environment: str | None = typer.Option(
        None,
        "--environment",
        help="Environment to run as; overrides the .itest/environment binding.",
    ),
) -> None:
    """Run the test suite and report point-level coverage."""
    from itest.core import environments, verifier

    base_dir = Path.cwd()
    try:
        report = verifier.run_verify(
            base_dir,
            output=output,
            redact_accounts=redact,
            environment=environment,
        )
    except (verifier.VerifyConfigError, environments.EnvironmentConfigError) as exc:
        # A bad policy or an undefined binding is a config problem, like a
        # missing manifest: exit 2, and never run the suite.
        echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    if output == "json":
        typer.echo(report.model_dump_json(indent=2))
    elif output == "junit":
        # The note names a file for a machine to pick up; the rollup after it
        # is for the human, so only that one is styled.
        typer.echo(f"Wrote JUnit XML to {verifier.JUNIT_NAME}")
        echo(render_verify_line(report))
    else:
        # `run_verify` already pseudonymized the report when --redact was
        # given, so this styles text that is safe to share, never before.
        echo(verifier.render_human(report, redacted=redact))

    if report.exit_code != 0:
        raise typer.Exit(code=report.exit_code)


@app.command()
def redact(
    # B008: see the note on `plan` above — typer requires the call here.
    input_path: Path | None = typer.Argument(  # noqa: B008
        None,
        metavar="[INPUT]",
        help="Plan or state JSON to sanitize. Reads stdin when omitted or '-'.",
    ),
    out: Path | None = typer.Option(  # noqa: B008
        None,
        "-o",
        "--out",
        help="Where to write the sanitized copy. Writes stdout when omitted.",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Report findings and exit nonzero without writing anything.",
    ),
    output: str = typer.Option(
        "human", "--output", help="Findings format for --check: human or json."
    ),
) -> None:
    """Sanitize plan/state JSON so it is safe to share."""
    from itest.core import redact as redact_engine

    if input_path is None or str(input_path) == "-":
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        source = str(input_path)
        if not input_path.exists():
            echo(f"Input file not found: {source}", err=True)
            raise typer.Exit(code=2)
        raw = input_path.read_text(encoding="utf-8")

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        echo(f"{source} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=2) from None

    clean, findings = redact_engine.redact_document(document)

    if check:
        if output == "json":
            result = redact_engine.RedactionResult(
                finding_count=len(findings), findings=findings
            )
            typer.echo(result.model_dump_json(indent=2))
        else:
            echo(redact_engine.render_findings(findings))
        if findings:
            raise typer.Exit(code=1)
        return

    payload = json.dumps(clean, indent=2) + "\n"
    if out is None:
        typer.echo(payload, nl=False)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        # A status note on the machine channel, like the JUnit note: stderr
        # carries it so stdout stays a clean pipe, and it stays unstyled.
        typer.echo(
            f"Wrote sanitized copy to {out} ({len(findings)} redaction(s)).",
            err=True,
        )


def render_verify_line(report) -> str:
    line = (
        f"{report.total_points} integration points: "
        f"{report.passing} passing, {report.failing} failing, "
        f"{report.stubs} stubs, {report.orphaned_tests} orphaned tests"
    )
    # Same append-only fragment the human rollup uses, so the junit path
    # reports gating too without perturbing the ungated line.
    if report.gated:
        line += f", {report.gated} gated"
    return line + "."


if __name__ == "__main__":
    app()
