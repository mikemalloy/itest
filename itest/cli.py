from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from itest import __version__

app = typer.Typer(
    help="ITest — analyze Terraform, extract integration points, verify infrastructure.",
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
    tf_json: Optional[Path] = typer.Option(
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
        raise typer.Exit(code=1)

    if output == "json":
        typer.echo(changeset.model_dump_json(indent=2))
    else:
        typer.echo(planner.render_changeset(changeset))


@app.command()
def sync(
    auto_approve: bool = typer.Option(
        False, "--auto-approve", help="Apply without an interactive prompt."
    ),
    tf_json: Optional[Path] = typer.Option(
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
        raise typer.Exit(code=1)

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
        raise typer.Exit(code=2)

    if output == "json":
        typer.echo(report.model_dump_json(indent=2))
    elif output == "junit":
        typer.echo(f"Wrote JUnit XML to {verifier.JUNIT_NAME}")
        typer.echo(render_verify_line(report))
    else:
        typer.echo(verifier.render_human(report))

    if report.exit_code != 0:
        raise typer.Exit(code=report.exit_code)


def render_verify_line(report) -> str:
    return (
        f"{report.total_points} integration points: "
        f"{report.passing} passing, {report.failing} failing, "
        f"{report.stubs} stubs, {report.orphaned_tests} orphaned tests."
    )


if __name__ == "__main__":
    app()
