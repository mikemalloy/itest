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
    except _declaration_errors() as exc:
        # A declaration ITest cannot act on: a mutation class the live server
        # contradicts, a trait the table cannot place, a malformed file. Exit 2
        # (a config problem, like a bad environment policy) and nothing written.
        echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

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
    allow_unreachable: bool = typer.Option(
        False,
        "--allow-unreachable",
        help=(
            "Apply even when a declared server cannot be reached. Its recorded "
            "points and tests are held exactly as they are."
        ),
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
    except _declaration_errors() as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    if note:
        echo(note)
    echo(planner.render_changeset(changeset))
    echo("")

    # A server that could not be asked is a failed sync, not a quiet partial
    # one: nothing is written unless the user says the gap is acceptable.
    if changeset.unreachable_servers and not allow_unreachable:
        names = ", ".join(sorted(changeset.unreachable_servers))
        echo(
            f"Not applied: declared server(s) unreachable: {names}. "
            "Make them reachable, or pass --allow-unreachable to apply the rest "
            "and hold their recorded points and tests as they are.",
            err=True,
        )
        raise typer.Exit(code=1)

    if syncer.is_noop(changeset):
        # Nothing to apply, but a stub implemented by hand since the last run
        # still has to be recorded: status is derived from the body, not from
        # whether the plan moved. Likewise an owned binding frozen against a
        # schema the manifest has since moved past is ITest's to regenerate.
        # And a per-tool stub for a trait the engine module now runs is
        # retired even when no plan moved.
        # First of all, a manifest read with old trait ids is written back
        # with the new ones.
        migrated = syncer.persist_migration(base_dir)
        regenerated = syncer.regenerate(base_dir)
        superseded = syncer.retire_superseded(base_dir)
        reclassified = syncer.reconcile(base_dir)
        if migrated:
            echo("Rewrote the manifest with the current trait ids.")
        if regenerated:
            echo(
                f"Regenerated {regenerated} check(s) against their tool's "
                "current schema."
            )
        if superseded:
            echo(f"Retired {superseded} per-tool stub(s) the engine module supersedes.")
        if reclassified:
            echo(f"Reclassified {reclassified} test(s) from their bodies.")
        if not (migrated or regenerated or superseded or reclassified):
            echo("No changes to apply. Manifest is up to date.")
        return

    if not auto_approve:
        # click writes the prompt itself, so it is decorated rather than
        # echoed; on a non-terminal `decorate` hands back the same string.
        if not typer.confirm(style.decorate(CONFIRM_PROMPT)):
            echo("Apply cancelled.")
            raise typer.Exit(code=1)

    try:
        result = syncer.apply(changeset, base_dir)
    except _declaration_errors() as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
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
        typer.echo(report.to_json(indent=2))
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
def report(
    html: bool = typer.Option(
        True, "--html", help="Render the HTML readiness page (the only format today)."
    ),
    # B008: see the note on `plan` above — typer requires the call here.
    out: Path | None = typer.Option(  # noqa: B008
        None, "--out", help="Where to write the page. Default: readiness.html."
    ),
    # `--out` is the file-path flag here and on `redact`; `--output` means an
    # output *format* on plan and verify. `report --output PATH` is accepted for
    # one release, with a warning, then removed.
    output_path: Path | None = typer.Option(  # noqa: B008
        None, "--output", hidden=True, help="Deprecated: use --out."
    ),
    environment: str | None = typer.Option(
        None,
        "--environment",
        help=(
            "Environment the report's verify runs as, exactly as "
            "`itest verify --environment`; overrides the .itest/environment binding."
        ),
    ),
    from_json: Path | None = typer.Option(  # noqa: B008
        None,
        "--from",
        help="A `verify --output json` document to render. Runs verify if omitted.",
    ),
    manifest: Path | None = typer.Option(  # noqa: B008
        None, "--manifest", help="Manifest to read. Defaults to .itest/manifest.yaml."
    ),
    since: Path | None = typer.Option(  # noqa: B008
        None,
        "--since",
        help="A prior manifest. Only this turns on trends and the since-line.",
    ),
    redact: bool = typer.Option(
        False, "--redact", help="Pseudonymize AWS account IDs, for safe sharing."
    ),
) -> None:
    """Render the release readiness page from verify's results."""
    from itest.core import environments, planner, verifier
    from itest.core import manifest as manifest_module
    from itest.core import redact as redact_engine
    from itest.report import model as report_model
    from itest.report import render as report_render

    if output_path is not None:
        if out is not None and out != output_path:
            echo(
                f"--out {out} and --output {output_path} name different files. "
                "Pass --out alone: --output is its deprecated alias.",
                err=True,
            )
            raise typer.Exit(code=2)
        typer.echo(
            "warning: `itest report --output` is deprecated and will be removed in "
            "the next release; use --out (on plan and verify, --output is a format).",
            err=True,
        )
        out = output_path
    out = out if out is not None else Path("readiness.html")
    if environment is not None and from_json is not None:
        echo(
            "--environment chooses where the report's own verify runs; --from "
            "renders a run that already happened, whose environment is in the "
            "document. Pass one or the other.",
            err=True,
        )
        raise typer.Exit(code=2)

    base_dir = Path.cwd()
    manifest_file = manifest or planner.manifest_path(base_dir)
    if not manifest_file.exists():
        echo(f"No manifest found at {manifest_file}.", err=True)
        raise typer.Exit(code=2)

    try:
        if from_json is None:
            # The same in-process path `verify --output json` prints, so the
            # page and that command can never disagree about a run.
            document = json.loads(
                verifier.run_verify(
                    base_dir,
                    output="json",
                    redact_accounts=redact,
                    environment=environment,
                ).model_dump_json()
            )
        else:
            if not from_json.exists():
                echo(f"No verify JSON found at {from_json}.", err=True)
                raise typer.Exit(code=2)
            raw = from_json.read_text(encoding="utf-8")
            if redact:
                # Verify's own scrubber, over the whole document: no second
                # redaction implementation can drift from the first.
                raw = redact_engine.text_scrubber()(raw)
            document = json.loads(raw)
    except (verifier.VerifyConfigError, environments.EnvironmentConfigError) as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    except json.JSONDecodeError as exc:
        echo(f"{from_json} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=2) from None

    prior = manifest_module.load_manifest(since) if since else None
    page = report_model.build(
        document,
        manifest_module.load_manifest(manifest_file),
        prior=prior,
        redacted=redact,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_render.render(page), encoding="utf-8")

    # A status note about a file, like the one `redact` writes: unstyled, and
    # it never carries the verdict into the exit code — that is verify's job.
    typer.echo(f"Wrote {out} ({out.stat().st_size} bytes).")
    typer.echo(
        f"Verdict: {page.verdict.word} — {page.verdict.integrations_verified} of "
        f"{page.verdict.integrations_total} integration points verified."
    )


@app.command()
def add(
    # B008: typer's declarative API requires the Option() call in the default;
    # `...` marks the option required — the caller states each one explicitly.
    point: str = typer.Option(  # noqa: B008
        ...,
        "--point",
        help=(
            "Existing integration point id to register onto, or the tool's name "
            "when --server is given."
        ),
    ),
    server: str | None = typer.Option(  # noqa: B008
        None,
        "--server",
        help=(
            "Declared MCP server. Reads --point as a tool name on that server "
            "instead of a point id."
        ),
    ),
    file: Path = typer.Option(  # noqa: B008
        ..., "--file", help="Path to the test file (must already exist)."
    ),
    function: str = typer.Option(  # noqa: B008
        ..., "--function", help="Name of the test function defined in that file."
    ),
    tier: str = typer.Option(  # noqa: B008
        ...,
        "--tier",
        help="Tier this test runs as: static, readonly, or active. Required.",
    ),
) -> None:
    """Register an existing test function onto an existing integration point."""
    from itest.core import register

    base_dir = Path.cwd()
    try:
        entry = register.add_test(
            base_dir,
            point_id=point,
            file=file,
            function=function,
            tier=tier,
            server=server,
        )
    except register.AddError as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    echo(f"Registered {entry.canonical} onto point {entry.point_id} (tier {tier}).")


@app.command()
def traits(
    for_tool: str | None = typer.Option(
        None,
        "--for",
        help=(
            "A declared tool, as <server>/<tool>: print whether each trait applies "
            "to it and the rule that decided."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Print the trait table, or every trait decided for one tool."""
    from itest.core import planner
    from itest.core import points as point_labels
    from itest.core.declarations.traits import (
        TraitTableError,
        load_traits,
        trait_decisions,
    )
    from itest.core.manifest import load_manifest
    from itest.traits import catalog

    try:
        table = load_traits()
    except TraitTableError as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    if for_tool is None:
        if as_json:
            typer.echo(json.dumps(catalog.table_json(table), indent=2))
        else:
            echo(catalog.render_table(table))
        return

    # Read from the manifest alone: what plan last recorded about the tool. No
    # declaration is loaded and no server is asked.
    manifest_file = planner.manifest_path(Path.cwd())
    if not manifest_file.exists():
        echo(
            f"No manifest found at {manifest_file}. Run `itest plan && itest sync` "
            "first: --for reads a tool's attributes from the manifest.",
            err=True,
        )
        raise typer.Exit(code=1)
    manifest = load_manifest(manifest_file)
    try:
        point = catalog.find_tool(manifest, for_tool)
        decisions = trait_decisions(point, table)
    except (catalog.ToolLookupError, TraitTableError) as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    if as_json:
        document = catalog.tool_json(point, decisions, table, manifest)
        typer.echo(json.dumps(document, indent=2))
    else:
        tag = point_labels.summary(point)
        echo(catalog.render_tool(point, decisions, table, manifest, tag))


@app.command()
def recipes(
    # B008: see the note on `plan` above — typer requires the call here.
    recipes_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--recipes-dir",
        help=(
            "Directory holding the skill's recipe files. Default: the first of "
            "skills/, .claude/skills/ and ~/.claude/skills/ that has "
            "itest-implementer/references/recipes."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List every recipe the trait table references, and whether it exists."""
    from itest.core.declarations.traits import TraitTableError, load_traits
    from itest.traits import catalog

    try:
        table = load_traits()
    except TraitTableError as exc:
        echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    base_dir = Path.cwd()
    directory = catalog.resolve_recipes_dir(recipes_dir, base_dir, Path.home())
    if as_json:
        document = catalog.recipes_json(table, directory, base_dir)
        typer.echo(json.dumps(document, indent=2))
    else:
        echo(catalog.render_recipes(table, directory, base_dir))


@app.command()
def standards(
    # B008: see the note on `plan` above — typer requires the call here.
    from_json: Path | None = typer.Option(  # noqa: B008
        None,
        "--from",
        help=(
            "A `verify --output json` document. Reads stdin when omitted, so "
            "`itest verify --output json | itest standards` works."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the rollup as JSON."),
) -> None:
    """Show this run's tool checks through the published standards they answer."""
    from itest.traits.standards import (
        StandardsCatalogError,
        render_rollup,
        rollup_for_ledger,
    )

    if from_json is None:
        if sys.stdin.isatty():
            echo(
                "Pass --from <verify --output json document>, or pipe one in: "
                "`itest verify --output json | itest standards`. No server is "
                "contacted; only the ledger and the shipped standards list are read.",
                err=True,
            )
            raise typer.Exit(code=2)
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        if not from_json.exists():
            echo(f"No verify JSON found at {from_json}.", err=True)
            raise typer.Exit(code=2)
        try:
            raw = from_json.read_text(encoding="utf-8")
        except OSError as exc:  # a directory, no read permission
            echo(f"{from_json} could not be read: {exc.strerror}", err=True)
            raise typer.Exit(code=2) from None
        source = str(from_json)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        echo(f"{source} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=2) from None
    ledger = document.get("tools") if isinstance(document, dict) else None
    if ledger is not None and not isinstance(ledger, dict):
        echo(
            f"{source}: `tools` is a {type(ledger).__name__}, not a mapping; "
            "expected the `tools` section of `itest verify --output json`.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        rollup = rollup_for_ledger(ledger)
    except StandardsCatalogError as exc:
        echo(f"{source}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    if as_json:
        typer.echo(json.dumps(rollup, indent=2))
    else:
        echo(render_rollup(rollup, ledger))


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


def _declaration_errors() -> tuple[type[Exception], ...]:
    """The declaration-side failures that map to exit code 2.

    Imported on demand, and only from the commands that can raise one, so a
    project with no declarations never loads the declarations package.
    """
    from itest.core.declarations import DeclarationError
    from itest.core.declarations.tools import DeclaredTraitUnknown, MutationConflict
    from itest.core.declarations.traits import TraitTableError

    return (DeclarationError, MutationConflict, DeclaredTraitUnknown, TraitTableError)


def render_verify_line(report) -> str:
    line = (
        f"{report.total_points} integration points: "
        f"{report.passing} passing, {report.failing} failing, "
        f"{report.stubs} stubs, {report.orphaned_tests} orphaned tests"
    )
    # Append-only fragments, so the common (no-error, no-gated) line stays
    # byte-identical: errored was silently dropped here, hiding a broken import
    # behind an exit-2 that named no error.
    if report.errored:
        line += f", {report.errored} errored"
    if report.gated:
        line += f", {report.gated} gated"
    return line + "."


if __name__ == "__main__":
    app()
