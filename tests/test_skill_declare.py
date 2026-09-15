"""The itest-declare skill ships in this repo, so what keeps it true is tested.

Three contracts, each guarded here because nothing else would notice them slip:

- **Field coverage.** The interview is written against the declaration schema.
  Every field of every model reachable from ``Declaration`` must have a row in
  ``references/field-map.md`` with one of four dispositions — asked, detected,
  derived, or default-confirmed — so a schema field added without an interview
  decision fails, and a row for a field that no longer exists fails too.
- **The walkthrough is real.** The transcript in ``references/walkthrough.md``
  ends in a declaration that must be the shipped
  ``examples/reference-mcp/.itest/tools/reference-mcp.yaml`` — the same facts,
  loaded through the same loader, yielding the same trait set per tool through
  the same code ``itest traits --for`` uses. A walkthrough that drifts from the
  exemplar is wrong, not merely stale.
- **The safety sentences are verbatim.** The rules the skill embodies (names
  never values, never production, the policy subset, never running sync) are
  asserted as exact text, so an edit that softens one fails.
"""

from __future__ import annotations

import re
import shutil
import types
import typing
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from test_skill_assets import parse_frontmatter

from itest.core.declarations import load_declarations
from itest.core.declarations.schema import Declaration
from itest.core.declarations.tools import build_points, build_target
from itest.core.declarations.traits import load_traits, trait_decisions

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "itest-declare"
SKILL_MD = SKILL_DIR / "SKILL.md"
INTERVIEW_MD = SKILL_DIR / "references" / "interview.md"
FIELD_MAP_MD = SKILL_DIR / "references" / "field-map.md"
WALKTHROUGH_MD = SKILL_DIR / "references" / "walkthrough.md"

EXAMPLE_DIR = REPO_ROOT / "examples" / "reference-mcp"
SHIPPED_DECLARATION = EXAMPLE_DIR / ".itest" / "tools" / "reference-mcp.yaml"
SHIPPED_POLICY = EXAMPLE_DIR / ".itest" / "environments.yaml"

#: The four dispositions a schema field may have in the field map. Nothing else
#: is accepted: a fifth word would be a way to leave a field undecided.
DISPOSITIONS = ("asked", "detected", "derived", "default, confirmed")

#: A question id as interview.md heads them and field-map.md cites them.
QUESTION_ID = re.compile(r"Q\d+\.\d+")

WALKTHROUGH_BEGIN = "<!-- BEGIN reference-mcp.yaml -->"
WALKTHROUGH_END = "<!-- END reference-mcp.yaml -->"

# The sentences the skill's conduct rests on. Verbatim: a paraphrase that
# drops a clause is exactly the edit this test exists to catch.
SAFETY_NAMES_ONLY = (
    "A URL or a credential is never written into the declaration. "
    "Environment variable names only."
)
SAFETY_PASTED_VALUE = (
    "If the user pastes a value, refuse to record it, say why — the file is "
    "committed and its errors get pasted into issues — and ask for the "
    "variable name instead."
)
SAFETY_NON_PRODUCTION = (
    "Name the non-production instance of this server, never a production one."
)
SAFETY_POLICY_SUBSET = (
    "`environments.active_allowed_in` must be a subset of what the committed "
    "policy already permits. Absence of a policy is not permission."
)
SAFETY_PRIVATE_HOSTS = (
    "A real deployment never needs it, and a reviewer should see it in the file."
)
SAFETY_STDIO_FACT = (
    "does this server check a credential of its own when launched, or does it "
    "trust whoever launched it?"
)
SAFETY_PLAN_ONLY_NETWORK = (
    "`itest plan` is the only step that touches the network, and it is read-only."
)
SAFETY_NEVER_SYNC = "This skill never runs `itest sync`."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _models_in(annotation: object) -> set[type[BaseModel]]:
    """Every pydantic model named anywhere inside a type annotation."""
    found: set[type[BaseModel]] = set()
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.add(annotation)
        return found
    origin = typing.get_origin(annotation)
    if origin is None:
        return found
    if origin is typing.Literal:
        return found
    for argument in typing.get_args(annotation):
        if isinstance(argument, types.UnionType):
            for member in typing.get_args(argument):
                found |= _models_in(member)
        else:
            found |= _models_in(argument)
    return found


def reachable_fields(root: type[BaseModel] = Declaration) -> set[str]:
    """``Model.field`` for every field of every model reachable from ``root``.

    Walked from the annotations, not hand-listed, so a new nested model or a
    new field on an existing one is picked up without anyone remembering to
    add it here.
    """
    seen: set[type[BaseModel]] = set()
    pending = [root]
    fields: set[str] = set()
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, info in model.model_fields.items():
            fields.add(f"{model.__name__}.{name}")
            for nested in _models_in(info.annotation):
                if nested not in seen:
                    pending.append(nested)
    return fields


def field_map_rows() -> dict[str, tuple[str, str]]:
    """``Model.field`` → ``(disposition, note)`` from the field map's table."""
    rows: dict[str, tuple[str, str]] = {}
    for line in FIELD_MAP_MD.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        match = re.fullmatch(r"`([A-Z][A-Za-z]+\.[a-z_]+)`", cells[0])
        if match is None:
            continue  # the header row and the separator
        key = match.group(1)
        assert key not in rows, f"{key} appears twice in field-map.md"
        rows[key] = (cells[2], cells[3])
    return rows


def interview_question_ids() -> set[str]:
    text = INTERVIEW_MD.read_text(encoding="utf-8")
    return set(re.findall(r"^### (Q\d+\.\d+)\b", text, flags=re.MULTILINE))


def walkthrough_declaration() -> str:
    """The YAML the walkthrough says the interview produced."""
    text = WALKTHROUGH_MD.read_text(encoding="utf-8")
    assert WALKTHROUGH_BEGIN in text, f"walkthrough.md lacks {WALKTHROUGH_BEGIN}"
    assert WALKTHROUGH_END in text, f"walkthrough.md lacks {WALKTHROUGH_END}"
    start = text.index(WALKTHROUGH_BEGIN) + len(WALKTHROUGH_BEGIN)
    block = text[start : text.index(WALKTHROUGH_END)].strip()
    assert block.startswith("```yaml"), "the resulting file must be a ```yaml fence"
    return block.split("\n", 1)[1].rsplit("```", 1)[0]


def normalise_comments(text: str) -> str:
    """The file with every comment and blank line removed.

    What is left is the facts. Two declarations equal here state the same
    thing, whatever their comments say.
    """
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if stripped.strip():
            kept.append(stripped)
    return "\n".join(kept) + "\n"


def shipped_declaration() -> Declaration:
    return next(
        d for d in load_declarations(EXAMPLE_DIR) if d.server == "reference-mcp"
    )


def trait_sets(declaration: Declaration, tools: list) -> dict[str, list[str]]:
    """``tool → [applying trait ids]``, decided as ``itest traits --for`` does."""
    table = load_traits()
    return {
        point.target: [d.trait.id for d in trait_decisions(point, table) if d.applies]
        for point in build_points(declaration, tools, table)
    }


@pytest.fixture(scope="module")
def live_tools() -> list:
    """The reference server's listing over stdio, once for the module."""
    from itest.probes.mcp import list_tools

    target = build_target(shipped_declaration(), EXAMPLE_DIR)
    assert target is not None
    return list_tools(target, base_dir=EXAMPLE_DIR)


# --------------------------------------------------------------------------
# Files and frontmatter
# --------------------------------------------------------------------------


def test_skill_files_exist() -> None:
    for path in (SKILL_MD, INTERVIEW_MD, FIELD_MAP_MD, WALKTHROUGH_MD):
        assert path.is_file(), f"missing {path}"


def test_skill_frontmatter_parses() -> None:
    data = parse_frontmatter(SKILL_MD)
    assert data.get("name") == "itest-declare"
    description = str(data.get("description", "")).strip()
    assert description, "frontmatter must carry a description"
    # The phrases a tool owner reaches for. A description that omits them is a
    # skill that never triggers.
    for trigger in (
        "onboard an MCP server",
        "declare a server",
        "write a declaration",
        ".itest/tools",
        "add my MCP server to itest",
        "what does ITest need to know about my server",
    ):
        assert trigger in description, f"description never mentions {trigger!r}"


# --------------------------------------------------------------------------
# Field coverage: the interview against the schema
# --------------------------------------------------------------------------


def test_every_schema_field_has_a_disposition() -> None:
    """A schema field with no interview entry is a fact the skill would never
    ask about and never explain. This is the test that keeps the skill honest
    as the schema grows."""
    expected = reachable_fields()
    assert expected, "no fields reachable from Declaration; the walk is broken"
    rows = field_map_rows()
    missing = sorted(expected - set(rows))
    assert not missing, f"schema fields with no row in field-map.md: {missing}"


def test_no_field_map_row_names_a_field_the_schema_lacks() -> None:
    """The reverse drift: a row for a field that was removed or renamed."""
    stale = sorted(set(field_map_rows()) - reachable_fields())
    assert not stale, f"field-map.md rows naming no schema field: {stale}"


def test_every_disposition_is_one_of_the_four() -> None:
    for key, (disposition, _) in sorted(field_map_rows().items()):
        assert disposition in DISPOSITIONS, f"{key}: {disposition!r}"


def test_every_asked_field_cites_a_question_the_interview_defines() -> None:
    """``asked`` without a question id is a promise the interview cannot keep."""
    defined = interview_question_ids()
    assert defined, "interview.md defines no ### Qn.m headings"
    for key, (disposition, note) in sorted(field_map_rows().items()):
        if disposition != "asked":
            continue
        cited = set(QUESTION_ID.findall(note))
        assert cited, f"{key} is asked but its row cites no question id"
        unknown = sorted(cited - defined)
        assert not unknown, f"{key} cites questions interview.md lacks: {unknown}"


def test_the_mutation_class_is_detected_never_asked() -> None:
    """Rule one: facts in, traits out. The dangerous field is never a question."""
    rows = field_map_rows()
    assert rows["Defaults.mutation"][0] == "detected"
    # A per-tool class is written only as a conflict resolution the owner made
    # after seeing both signals, and that is a question, not a classification.
    disposition, note = rows["ToolOverride.mutation"]
    assert disposition == "asked"
    assert "conflict" in note.lower()


def test_defaults_active_is_a_confirmed_default() -> None:
    assert field_map_rows()["Defaults.active"][0] == "default, confirmed"


def test_the_counts_table_matches_the_rows() -> None:
    """The summary counts are read by people; they must not drift from the rows."""
    rows = field_map_rows()
    counted = {
        d: sum(1 for disposition, _ in rows.values() if disposition == d)
        for d in DISPOSITIONS
    }
    text = FIELD_MAP_MD.read_text(encoding="utf-8")
    counts_row = re.compile(
        r"^\| (asked|detected|derived|default, confirmed) \| (\d+) \|$", re.MULTILINE
    )
    stated = dict(counts_row.findall(text))
    assert {k: int(v) for k, v in stated.items()} == counted


# --------------------------------------------------------------------------
# The question bank
# --------------------------------------------------------------------------


def test_every_question_says_what_it_fills_why_and_what_unknown_means() -> None:
    """Each entry carries the four parts the skill shows or acts on."""
    text = INTERVIEW_MD.read_text(encoding="utf-8")
    headings = list(re.finditer(r"^### (Q\d+\.\d+)\b.*$", text, flags=re.MULTILINE))
    assert headings, "interview.md defines no questions"
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[match.end() : end]
        for part in ("**Fills:**", "**Ask:**", "**Why:**", '**If "don\'t know":**'):
            assert part in body, f"{match.group(1)} lacks {part}"


def test_the_interview_never_asks_for_a_classification() -> None:
    """The phrasings the design forbids, checked in the questions themselves."""
    text = INTERVIEW_MD.read_text(encoding="utf-8")
    asks = re.findall(r"\*\*Ask:\*\*(.*?)(?=\n- \*\*|\n###|\Z)", text, flags=re.DOTALL)
    assert asks
    for ask in asks:
        lowered = ask.lower()
        assert "is this tool destructive" not in lowered, ask
        assert "which traits apply" not in lowered, ask


# --------------------------------------------------------------------------
# The walkthrough against the shipped exemplar
# --------------------------------------------------------------------------


def test_the_walkthrough_declaration_is_the_shipped_one() -> None:
    """Byte for byte after comment normalisation."""
    produced = normalise_comments(walkthrough_declaration())
    shipped = normalise_comments(SHIPPED_DECLARATION.read_text(encoding="utf-8"))
    assert produced == shipped


def test_the_walkthrough_declaration_loads_and_equals_the_shipped_model(
    tmp_path: Path,
) -> None:
    """Loaded through the real loader, beside the real policy: the same facts."""
    tools_dir = tmp_path / ".itest" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "reference-mcp.yaml").write_text(
        walkthrough_declaration(), encoding="utf-8"
    )
    shutil.copy(SHIPPED_POLICY, tmp_path / ".itest" / "environments.yaml")
    loaded = load_declarations(tmp_path)
    assert len(loaded) == 1
    assert loaded[0] == shipped_declaration()


def test_the_walkthrough_yields_the_shipped_trait_set_per_tool(
    tmp_path: Path, live_tools: list
) -> None:
    """Decided by the same code `itest traits --for` runs, from a live listing."""
    tools_dir = tmp_path / ".itest" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "reference-mcp.yaml").write_text(
        walkthrough_declaration(), encoding="utf-8"
    )
    shutil.copy(SHIPPED_POLICY, tmp_path / ".itest" / "environments.yaml")
    produced = trait_sets(load_declarations(tmp_path)[0], live_tools)
    shipped = trait_sets(shipped_declaration(), live_tools)
    assert produced == shipped
    assert set(produced) == {t.name for t in live_tools}


@pytest.mark.parametrize("tool", ["delete_record", "enrich"])
def test_the_walkthrough_shows_the_trait_set_it_actually_unlocks(
    tool: str, live_tools: list
) -> None:
    """The `itest traits --for` excerpts in the walkthrough are real output:
    every trait shown as APPLIES applies, and none that applies is left out."""
    text = WALKTHROUGH_MD.read_text(encoding="utf-8")
    marker = f"reference-mcp/{tool}:"
    assert marker in text, f"walkthrough.md shows no `itest traits --for` for {tool}"
    excerpt = text[text.index(marker) :]
    excerpt = excerpt[: excerpt.index("```")]
    shown = re.findall(r"^\s*APPLIES\s+(\S+)", excerpt, flags=re.MULTILINE)
    assert shown, f"no APPLIES rows in the {tool} excerpt"
    expected = trait_sets(shipped_declaration(), live_tools)[tool]
    assert shown == expected


def test_the_walkthrough_answers_come_from_the_server() -> None:
    """The answers the transcript gives are the reference server's facts."""
    text = WALKTHROUGH_MD.read_text(encoding="utf-8")
    for fact in (
        "REFERENCE_MCP_TOKEN_TENANT_B",
        "search_records",
        "confirm",
        "enrichment-provider.example",
        "sentinel-cannot-exist-0000",
        "stderr-json",
    ):
        assert fact in text, f"walkthrough.md never mentions {fact}"
    # And the transcript shows the re-run asking nothing.
    assert "re-run" in text.lower()


# --------------------------------------------------------------------------
# The safety sentences
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        SAFETY_NAMES_ONLY,
        SAFETY_PASTED_VALUE,
        SAFETY_NON_PRODUCTION,
        SAFETY_POLICY_SUBSET,
        SAFETY_PRIVATE_HOSTS,
        SAFETY_STDIO_FACT,
        SAFETY_PLAN_ONLY_NETWORK,
        SAFETY_NEVER_SYNC,
    ],
)
def test_skill_carries_the_safety_sentence_verbatim(sentence: str) -> None:
    """Verbatim modulo markdown line-wrapping: runs of whitespace are one space."""
    text = re.sub(r"\s+", " ", SKILL_MD.read_text(encoding="utf-8"))
    assert sentence in text, sentence


def test_skill_lists_detected_destructive_tools_when_asking_about_gates() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "destructive" in text
    assert "by name" in text


def test_skill_has_a_re_run_mode_that_says_what_did_not_change() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "## Re-run mode" in text
    assert "did not change" in text.lower()
    assert "orphaned" in text.lower()


def test_skill_never_resolves_a_conflict_itself() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "Never resolve one yourself." in text


def test_skill_ends_by_handing_off_to_sync_not_running_it() -> None:
    """The skill's own commands never include sync; the hand-off names it."""
    text = SKILL_MD.read_text(encoding="utf-8")
    fence = re.compile(r"```sh\n(.*?)```", re.DOTALL)
    commands = "\n".join(fence.findall(text))
    for line in commands.splitlines():
        if line.strip().startswith("itest "):
            assert not line.strip().startswith("itest sync"), line
    assert "itest sync" in text


def test_skill_reads_the_shipped_example_as_its_exemplar() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "examples/reference-mcp/.itest/tools/reference-mcp.yaml" in text


def test_the_walkthrough_file_is_valid_yaml() -> None:
    document = yaml.safe_load(walkthrough_declaration())
    assert document["server"] == "reference-mcp"
