from __future__ import annotations

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
def plan() -> None:
    """Detect integration points and propose a changeset."""
    typer.echo("not implemented")
    raise typer.Exit(code=1)


@app.command()
def sync() -> None:
    """Apply the plan: update the manifest and generate test stubs."""
    typer.echo("not implemented")
    raise typer.Exit(code=1)


@app.command()
def verify() -> None:
    """Run the test suite and report point-level coverage."""
    typer.echo("not implemented")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
