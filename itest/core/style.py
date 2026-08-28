"""Terminal styling for ITest's human output — a colorizer, never a rewrite.

The render functions (`planner.render_changeset`, `verifier.render_human`,
`redact.render_findings`, `cli.render_verify_line`) are the single source of
what ITest says. This module takes their finished text and decides which spans
of it are bold, dim, or coloured. It never re-lays-out content, so the
contract is one testable invariant:

    strip_ansi(decorate(text)) == text

Everything else follows from that. Styling is applied only when stdout (or
stderr) is a terminal, which is Rich's own detection — nothing here forces a
terminal except the documented ``ITEST_FORCE_COLOR`` escape hatch, which
exists so a demo or an acceptance run can capture colour through a pipe.
``NO_COLOR`` and the CLI's ``--no-color`` switch styling off entirely: the
caller then gets the canonical string back unchanged, and the CLI hands that
to ``typer.echo`` exactly as it always did.

Two properties matter for byte-identity and are easy to lose:

- **No wrapping.** A Rich console attached to a non-terminal defaults to 80
  columns, and the module-nested addresses in a real plan run past 180. Every
  print here passes ``soft_wrap=True``, which disables wrapping, cropping and
  padding alike.
- **No markup parsing.** Canonical text is full of square brackets — point
  tags, count indices, status flags. ``markup=False`` and ``highlight=False``
  keep Rich from reading any of it as instructions.

The styling itself is deliberately restrained. This is a tool engineers read
in a terminal next to `terraform plan`, not a dashboard: rollups carry weight,
finding-class flags carry colour, and everything else stays the colour of the
terminal.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from rich.console import Console
from rich.text import Text

#: Set to any value to style output even when stdout is not a terminal. The
#: one sanctioned use is capturing coloured output for a demo or an
#: acceptance run; nothing in normal operation sets it.
FORCE_COLOR_ENV = "ITEST_FORCE_COLOR"

#: https://no-color.org. Presence disables styling; the value is not read.
NO_COLOR_ENV = "NO_COLOR"

#: Sentinel style key meaning "the whole line", as opposed to a match group.
LINE = "__line__"

_disabled = False
_consoles: dict[str, Console] = {}


# --------------------------------------------------------------------------
# Consoles
# --------------------------------------------------------------------------


def configure(no_color: bool = False) -> None:
    """Re-read the environment for this invocation.

    Called from the CLI callback, so ``--no-color`` and the environment are
    picked up per run rather than frozen at import time.
    """
    global _disabled
    _disabled = no_color
    _consoles.clear()


def _console(key: str, **kwargs) -> Console:
    if key not in _consoles:
        _consoles[key] = Console(
            # None means "detect", which is what Rich does with the real
            # stream. Only the documented escape hatch overrides it.
            force_terminal=True if os.environ.get(FORCE_COLOR_ENV) else None,
            markup=False,
            highlight=False,
            emoji=False,
            soft_wrap=True,
            **kwargs,
        )
    return _consoles[key]


def stream_console(err: bool = False) -> Console:
    """The console bound to a real stream. Used only to detect a terminal.

    ``Console.file`` is resolved lazily from ``sys.stdout``/``sys.stderr`` on
    every access, so a test runner that replaces the stream is seen correctly.
    """
    return _console("err" if err else "out", stderr=err)


def _recording_console() -> Console:
    """The console that turns a styled ``Text`` into an ANSI string.

    Records to a throwaway buffer rather than to a stream: `render_ansi` has
    to produce the same markup whether or not the caller is a terminal (the
    prompt text handed to ``typer.confirm`` never touches this console's
    file), and the terminal question is already settled by `enabled`.
    """
    return _console("record", file=io.StringIO(), record=True)


def enabled(err: bool = False) -> bool:
    """True when output on this stream should carry ANSI styling."""
    if _disabled or NO_COLOR_ENV in os.environ:
        return False
    return stream_console(err).is_terminal


# --------------------------------------------------------------------------
# The rule table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One line pattern and the styles it paints onto the groups it matched.

    ``styles`` covers the fixed cases; ``refine`` the two counts whose colour
    depends on their value; ``spans`` the one case a regex cannot express,
    a point tag whose brackets nest.
    """

    name: str
    pattern: re.Pattern[str]
    styles: Mapping[str, str] = field(default_factory=dict)
    refine: Callable[[re.Match[str]], Iterable[tuple[str, str]]] | None = None
    spans: Callable[[str, re.Match[str]], Iterable[tuple[int, int, str]]] | None = None


def _matching_bracket(line: str, start: int) -> int | None:
    """Index just past the ``]`` closing the ``[`` at ``start``, or None.

    A point tag can carry brackets of its own — ``[HTTP:80 -> tg [priority 1]
    [weight 100]]`` — and so can the addresses after it, so neither a greedy
    nor a lazy regex finds the right closer. Depth counting does.
    """
    if start >= len(line) or line[start] != "[":
        return None
    depth = 0
    for index in range(start, len(line)):
        if line[index] == "[":
            depth += 1
        elif line[index] == "]":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _plan_new_spans(line: str, match: re.Match[str]) -> Iterable[tuple[int, int, str]]:
    """Green on the ``+`` marker and on the whole bracketed tag."""
    yield match.start("marker"), match.end("marker"), "green"
    open_at = line.find("[", match.end("marker"))
    if open_at != -1:
        end = _matching_bracket(line, open_at)
        if end is not None:
            yield open_at, end, "green"


def _verify_rollup_refine(match: re.Match[str]) -> Iterable[tuple[str, str]]:
    """Red the failing count when nonzero; green the passing count when clean.

    Green is withheld while anything errored: the suite could not run, so
    "all passing" would be a claim the run did not earn.
    """
    failing = int(match.group("failing_n"))
    errored = int(match.group("errored_n") or 0)
    if failing:
        yield ("failing", "bold red")
    elif not errored:
        yield ("passing", "bold green")


_POINT_STATUS_STYLE = {
    "PASS": "green",
    "FAIL": "bold red",
    "ERROR": "magenta",
    "STUB": "dim",
    # No verify path emits this today — point status is passing/failing/error
    # /stub — but orphaning is a manifest state the reporting may surface, and
    # a rule that never fires cannot change a byte of output.
    "ORPHAN": "yellow",
}


def _point_status_refine(match: re.Match[str]) -> Iterable[tuple[str, str]]:
    style_name = _POINT_STATUS_STYLE.get(match.group("status"))
    if style_name:
        yield ("tag", style_name)


#: Section headers, spelled out rather than pattern-guessed: these are strings
#: this codebase emits, so an exact list cannot drift into someone's data.
_SECTIONS = (
    "New integration points (",
    "Orphan candidates (",
    "Not analyzed (",
    "Resurrected (",
)

RULES: tuple[Rule, ...] = (
    Rule("plan_summary", re.compile(r"^ITest plan: "), {LINE: "bold"}),
    Rule(
        "verify_rollup",
        re.compile(
            r"^\d+ integration points: "
            r"(?P<passing>(?P<passing_n>\d+) passing), "
            r"(?P<failing>(?P<failing_n>\d+) failing), "
            r"(?:(?P<errored>(?P<errored_n>\d+) errored), )?"
            r"\d+ stubs, \d+ orphaned tests\.$"
        ),
        {LINE: "bold"},
        refine=_verify_rollup_refine,
    ),
    Rule("sync_applied", re.compile(r"^Applied: "), {LINE: "bold"}),
    # The one interactive moment in the tool, and the one styled string the
    # CLI owns rather than a render function.
    Rule("sync_prompt", re.compile(r"^Apply these changes\?$"), {LINE: "bold"}),
    Rule("sync_cancelled", re.compile(r"^Apply cancelled\.$"), {LINE: "yellow"}),
    Rule("sync_noop", re.compile(r"^No changes to apply\. "), {LINE: "bold"}),
    Rule("sync_reclassified", re.compile(r"^Reclassified \d+ test"), {LINE: "bold"}),
    Rule("redact_findings", re.compile(r"^\d+ finding\(s\) — "), {LINE: "bold"}),
    Rule("redact_clean", re.compile(r"^No secrets found\. "), {LINE: "bold"}),
    Rule(
        "plan_new",
        re.compile(r"^  (?P<marker>\+) \["),
        spans=_plan_new_spans,
    ),
    Rule("plan_orphan", re.compile(r"^  ~ "), {LINE: "yellow"}),
    Rule("plan_resurrected", re.compile(r"^  \^ \[returning\] "), {LINE: "cyan"}),
    Rule(
        "verify_point",
        re.compile(r"^  (?P<tag>\[(?P<status>[A-Z?]{4,6})\]) "),
        refine=_point_status_refine,
    ),
    Rule("verify_failures", re.compile(r"^Failing tests:$"), {LINE: "bold red"}),
    Rule(
        "verify_errors",
        re.compile(r"^Errored tests \(the suite could not run\):$"),
        {LINE: "bold magenta"},
    ),
    Rule("verify_points_header", re.compile(r"^Points:$"), {LINE: "bold"}),
    Rule(
        "verify_unregistered",
        re.compile(r"^Unregistered tests \(not in manifest\):$"),
        {LINE: "bold"},
    ),
    Rule(
        "section",
        re.compile("|".join(f"^{re.escape(s)}" for s in _SECTIONS)),
        {LINE: "bold"},
    ),
    # `redact --check` groups its findings under a lowercase category name.
    Rule("redact_category", re.compile(r"^[a-z][a-z0-9_]* \(\d+\):$"), {LINE: "bold"}),
    # Detail lines: a plan point's id/hcl, a finding's detail. Traceback lines
    # share the indent and are excluded before the rules ever run.
    Rule("detail", re.compile(r"^      \S"), {LINE: "dim"}),
)

#: Finding-class flags, painted on top of whatever rule claimed the line.
#: `external` is context — a cross-stack reference is a fact, not a finding —
#: so it is deliberately absent.
FLAGS = re.compile(
    r"\[open\]|\bBROAD\b|\bDENY\b|\bwildcard_action\b|\bwildcard_resource\b"
)

#: Once one of these headers is seen, following lines are pytest's own output
#: until the blank line that ends the block. It is already formatted, and it
#: can contain the very words FLAGS looks for.
_TRACEBACK_HEADERS = (
    "Failing tests:",
    "Errored tests (the suite could not run):",
)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(text: str, *, error: bool = False) -> Text:
    """Return ``text`` as a Rich ``Text`` carrying style spans and nothing else.

    The plain content is the input, unmodified — only spans are added.
    """
    rendered = Text(text)
    if error:
        # Anything ITest writes to stderr through the styled path is a
        # failure the user has to see; the message may span several lines.
        rendered.stylize("red", 0, len(text))
        return rendered

    offset = 0
    in_traceback = False
    for line in text.split("\n"):
        if in_traceback:
            if line == "":
                in_traceback = False
        elif line in _TRACEBACK_HEADERS:
            in_traceback = True

        if not in_traceback or line in _TRACEBACK_HEADERS:
            _stylize_line(rendered, offset, line)
        offset += len(line) + 1
    return rendered


def _stylize_line(rendered: Text, offset: int, line: str) -> None:
    for rule in RULES:
        match = rule.pattern.search(line)
        if match is None:
            continue
        for key, style_name in rule.styles.items():
            _paint(rendered, offset, line, match, key, style_name)
        if rule.refine is not None:
            for key, style_name in rule.refine(match):
                _paint(rendered, offset, line, match, key, style_name)
        if rule.spans is not None:
            for start, end, style_name in rule.spans(line, match):
                rendered.stylize(style_name, offset + start, offset + end)
        break

    for flag in FLAGS.finditer(line):
        rendered.stylize("yellow", offset + flag.start(), offset + flag.end())


def _paint(
    rendered: Text,
    offset: int,
    line: str,
    match: re.Match[str],
    key: str,
    style_name: str,
) -> None:
    if key == LINE:
        start, end = 0, len(line)
    else:
        start, end = match.span(key)
        if start < 0:
            return
    rendered.stylize(style_name, offset + start, offset + end)


def render_ansi(text: str, *, error: bool = False) -> str:
    """Return ``text`` with ANSI escapes around its styled spans.

    Ungated on purpose: `decorate` decides whether styling applies, and the
    tests need the styled form regardless of whether they run on a terminal.
    """
    if not text:
        return text
    console = _recording_console()
    console.print(render(text, error=error), soft_wrap=True, end="")
    return console.export_text(styles=True)


def decorate(text: str, *, err: bool = False) -> str:
    """Style ``text`` when this stream should be styled, else return it as-is.

    The disabled branch returns the argument itself, so the caller's
    ``typer.echo`` writes exactly the bytes it wrote before this module
    existed.
    """
    if not enabled(err):
        return text
    return render_ansi(text, error=err)
