from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from itest import __version__

app = typer.Typer(
    help=(
        "ITest — analyze Terraform, extract integration points, verify infrastructure."
    ),
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the ITest version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ITest command-line interface."""


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
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    if output == "json":
        typer.echo(changeset.model_dump_json(indent=2))
    else:
        typer.echo(planner.render_changeset(changeset))


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
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    if note:
        typer.echo(note)
    typer.echo(planner.render_changeset(changeset))
    typer.echo("")

    if syncer.is_noop(changeset):
        typer.echo("No changes to apply. Manifest is up to date.")
        return

    if not auto_approve:
        if not typer.confirm("Apply these changes?"):
            typer.echo("Apply cancelled.")
            raise typer.Exit(code=1)

    result = syncer.apply(changeset, base_dir)
    typer.echo(result.summary())


@app.command()
def verify(
    output: str = typer.Option(
        "human", "--output", help="Output format: human, json, or junit."
    ),
) -> None:
    """Run the test suite and report point-level coverage."""
    from itest.core import verifier

    base_dir = Path.cwd()
    try:
        report = verifier.run_verify(base_dir, output=output)
    except verifier.VerifyConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    if output == "json":
        typer.echo(report.model_dump_json(indent=2))
    elif output == "junit":
        typer.echo(f"Wrote JUnit XML to {verifier.JUNIT_NAME}")
        typer.echo(render_verify_line(report))
    else:
        typer.echo(verifier.render_human(report))

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
            typer.echo(f"Input file not found: {source}", err=True)
            raise typer.Exit(code=2)
        raw = input_path.read_text(encoding="utf-8")

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"{source} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=2) from None

    clean, findings = redact_engine.redact_document(document)

    if check:
        if output == "json":
            result = redact_engine.RedactionResult(
                finding_count=len(findings), findings=findings
            )
            typer.echo(result.model_dump_json(indent=2))
        else:
            typer.echo(redact_engine.render_findings(findings))
        if findings:
            raise typer.Exit(code=1)
        return

    payload = json.dumps(clean, indent=2) + "\n"
    if out is None:
        typer.echo(payload, nl=False)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        typer.echo(
            f"Wrote sanitized copy to {out} ({len(findings)} redaction(s)).",
            err=True,
        )


def render_verify_line(report) -> str:
    return (
        f"{report.total_points} integration points: "
        f"{report.passing} passing, {report.failing} failing, "
        f"{report.stubs} stubs, {report.orphaned_tests} orphaned tests."
    )


if __name__ == "__main__":
    app()
