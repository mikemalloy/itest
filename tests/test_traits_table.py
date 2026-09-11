"""The applies-when table is data, and its expression language refuses to shrug.

Two properties matter more than the eleven rows that ship:

1. **A typo cannot silently delete a check.** An unknown attribute, an
   unreadable clause, an empty expression and a table at the wrong version are
   all errors that name what is wrong. A clause that quietly evaluated to
   ``False`` would withhold a check, and nothing downstream would ever say so.
2. **The shipped table says what the design says it says.** The row set, the
   tiers and the recipes are pinned here, so a change to the policy is a visible
   change to this file as well as to the YAML.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from itest.core.declarations.tools import has_free_form_input, resolve_mutation
from itest.core.declarations.traits import (
    KNOWN_ATTRIBUTES,
    TraitTableError,
    applicable,
    evaluate,
    load_traits,
    trait_table_hash,
)

CONTEXT = {
    "mutation": "write",
    "egress": None,
    "approval": "none",
    "active": True,
    "has_free_form_input": True,
    "auth.second_tenant_env": "TENANT_B",
    "audit.sink": "stderr-json",
    "identity.runs_as": "service",
}


# --- the shipped table --------------------------------------------------------


def test_the_shipped_table_is_the_fourteen_traits() -> None:
    table = load_traits()
    assert table.ids == [
        "authority.anonymous",
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "authority.delegation",
        "blast.mutation_class",
        "blast.destructive_gating",
        "blast.egress",
        "blast.audit",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ]
    assert table.families == {
        "authority": "Authority",
        "blast": "Blast radius",
        "containment": "Containment",
        "change": "Change",
    }
    # The tier is what the environment policy gates on, so it is policy too.
    assert {t.id for t in table.traits if t.tier == "active"} == {
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "authority.delegation",
        "blast.destructive_gating",
        "blast.audit",
        "containment.parameter_scope",
        "containment.expression_passthrough",
    }
    assert {t.recipe for t in table.traits if t.family == "change"} == {
        "tool_provenance.md"
    }


def test_the_kind_column_says_who_runs_each_check() -> None:
    """Engine traits run from the manifest with no per-tool file; generated
    traits get a thin per-tool binding stub. The split is the design doc's."""
    table = load_traits()
    kinds = {t.id: t.kind for t in table.traits}
    assert {k for k, v in kinds.items() if v == "engine"} == {
        "authority.anonymous",
        "blast.mutation_class",
        "blast.egress",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    }
    assert {k for k, v in kinds.items() if v == "generated"} == {
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "authority.delegation",
        "blast.destructive_gating",
        "blast.audit",
    }


def test_the_identity_and_hygiene_rows() -> None:
    table = load_traits()
    a3, a4, c3 = (
        table.get("authority.backing_least_privilege"),
        table.get("authority.delegation"),
        table.get("containment.output_hygiene"),
    )
    assert a3.applies_when == "identity.runs_as present"
    assert a4.applies_when == "identity.runs_as == passthrough"
    assert (a3.tier, a3.kind, a3.recipe) == ("active", "generated", "tool_identity.md")
    assert (a4.tier, a4.kind, a4.recipe) == ("active", "generated", "tool_identity.md")
    assert c3.name == "output hygiene"
    assert c3.applies_when == "mutation != informational"
    assert (c3.tier, c3.kind, c3.recipe) == (
        "readonly",
        "engine",
        "tool_containment.md",
    )


def test_every_shipped_expression_evaluates() -> None:
    """No row of the committed table may be unreadable by the evaluator."""
    table = load_traits()
    for trait in table.traits:
        assert isinstance(evaluate(trait.applies_when, CONTEXT), bool)


def test_an_informational_tool_gets_only_the_always_traits() -> None:
    context = dict(CONTEXT, mutation="informational", has_free_form_input=False)
    assert [t.id for t in applicable(load_traits(), context)] == [
        "authority.anonymous",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ]


# --- the expression language ---------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("always", True),
        ("mutation == write", True),
        ("mutation == read", False),
        ("mutation != informational", True),
        ("mutation in [write, destructive]", True),
        ("mutation in [read, informational]", False),
        ("audit.sink present", True),
        ("egress present", False),
        ("egress != none", False),
        ("has_free_form_input", True),
        ("active", True),
        ("mutation == write and audit.sink present", True),
        ("mutation == write and egress present", False),
        ("auth.second_tenant_env present and mutation != informational", True),
    ],
)
def test_the_grammar(expression: str, expected: bool) -> None:
    assert evaluate(expression, CONTEXT) is expected


def test_a_quoted_literal_and_a_boolean_literal_both_read() -> None:
    assert evaluate("mutation == 'write'", CONTEXT) is True
    assert evaluate("active == true", CONTEXT) is True
    assert evaluate("active != false", CONTEXT) is True


def test_an_unknown_attribute_is_an_error_not_a_false_clause() -> None:
    """The refusal that matters: a mistyped field must not quietly withhold a
    check that nobody then notices is missing."""
    with pytest.raises(TraitTableError) as excinfo:
        evaluate("mutatoin == write", CONTEXT)
    assert "mutatoin" in str(excinfo.value)
    assert "mutation" in str(excinfo.value)  # the known attributes are listed


def test_an_unreadable_clause_is_an_error() -> None:
    with pytest.raises(TraitTableError) as excinfo:
        evaluate("mutation is probably write", CONTEXT)
    assert "cannot read" in str(excinfo.value)


def test_an_empty_expression_is_an_error() -> None:
    with pytest.raises(TraitTableError):
        evaluate("   ", CONTEXT)


def test_there_is_no_or_and_it_says_so() -> None:
    """Only conjunction ships. An `or` is read as part of a clause and refused,
    rather than silently meaning something else."""
    with pytest.raises(TraitTableError):
        evaluate("mutation == write or mutation == read", CONTEXT)


# --- loading a table ----------------------------------------------------------


def _table(tmp_path: Path, document: object) -> Path:
    """Write a table. A dict without ``families`` gets families authority and
    blast, so a test about one refusal is not accidentally tripping another."""
    if isinstance(document, dict) and "families" not in document:
        document = {
            **document,
            "families": {"authority": "Authority", "blast": "Blast radius"},
        }
    path = tmp_path / "traits.yaml"
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    path.write_text(text, encoding="utf-8")
    return path


def _row(**changes: object) -> dict:
    row = {
        "id": "authority.anonymous",
        "code": "AUTH-1",
        "family": "authority",
        "name": "refuses anonymous",
        "applies_when": "always",
        "tier": "readonly",
        "kind": "engine",
        "recipe": "tool_authn.md",
    }
    row.update(changes)
    return row


def test_a_missing_table_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(tmp_path / "nope.yaml")
    assert "nope.yaml" in str(excinfo.value)


def test_a_table_path_that_is_a_directory_is_a_table_error(tmp_path: Path) -> None:
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(tmp_path)
    assert str(tmp_path) in str(excinfo.value)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root reads a mode-000 file anyway",
)
def test_an_unreadable_table_is_a_table_error(tmp_path: Path) -> None:
    path = tmp_path / "traits.yaml"
    path.write_text("version: 1\ntraits: []\n", encoding="utf-8")
    path.chmod(0)
    try:
        with pytest.raises(TraitTableError) as excinfo:
            load_traits(path)
    finally:
        path.chmod(0o600)
    assert "traits.yaml" in str(excinfo.value)


def test_a_table_at_another_version_is_refused(tmp_path: Path) -> None:
    path = _table(tmp_path, {"version": 2, "traits": []})
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    assert "version" in str(excinfo.value)


def test_an_empty_table_is_refused(tmp_path: Path) -> None:
    path = _table(tmp_path, {"version": 1, "traits": []})
    with pytest.raises(TraitTableError):
        load_traits(path)


def test_a_duplicate_trait_id_is_refused(tmp_path: Path) -> None:
    path = _table(tmp_path, {"version": 1, "traits": [_row(), _row()]})
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    assert "authority.anonymous" in str(excinfo.value)


def test_an_unknown_key_in_a_row_is_refused(tmp_path: Path) -> None:
    path = _table(tmp_path, {"version": 1, "traits": [_row(danger=True)]})
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    assert "danger" in str(excinfo.value)


def test_an_unknown_tier_is_refused(tmp_path: Path) -> None:
    path = _table(tmp_path, {"version": 1, "traits": [_row(tier="whenever")]})
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    message = str(excinfo.value)
    assert "whenever" in message and "authority.anonymous" in message
    assert "static, readonly, active" in message  # the policy's tiers


def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    path = _table(tmp_path, {"version": 1, "traits": [_row(kind="handwritten")]})
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    message = str(excinfo.value)
    assert "handwritten" in message and "authority.anonymous" in message
    assert "engine" in message and "generated" in message


def test_a_row_without_a_kind_is_refused(tmp_path: Path) -> None:
    row = _row()
    del row["kind"]
    path = _table(tmp_path, {"version": 1, "traits": [row]})
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    assert "kind" in str(excinfo.value)


def test_an_unknown_family_is_refused(tmp_path: Path) -> None:
    path = _table(tmp_path, {"version": 1, "traits": [_row(family="Z")]})
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    message = str(excinfo.value)
    assert "'Z'" in message and "authority.anonymous" in message
    assert "authority, blast" in message  # the families the table does define


def test_an_unparseable_applies_when_is_refused_at_load(tmp_path: Path) -> None:
    """Not at first use: a table whose rule cannot be read must not load, or
    it would only fail on the first sync that reached that row."""
    path = _table(
        tmp_path,
        {"version": 1, "traits": [_row(applies_when="mutation is probably write")]},
    )
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    assert "authority.anonymous" in str(excinfo.value)
    assert "cannot read" in str(excinfo.value)


def test_an_applies_when_naming_an_unknown_attribute_is_refused_at_load(
    tmp_path: Path,
) -> None:
    path = _table(
        tmp_path,
        {"version": 1, "traits": [_row(applies_when="mutatoin == write")]},
    )
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    assert "mutatoin" in str(excinfo.value)
    assert "authority.anonymous" in str(excinfo.value)


def test_the_known_attributes_are_exactly_what_a_point_supplies() -> None:
    """The load-time check and the evaluation context cannot disagree: a field
    the loader accepts is one every tool point carries, and vice versa."""
    from itest.core.declarations.traits import trait_context
    from itest.core.manifest import IntegrationPoint

    point = IntegrationPoint(
        id="x",
        type="mcp_tool",
        source="s",
        target="t",
        hcl_address="h",
        first_seen="2026-01-01T00:00:00Z",
        last_seen="2026-01-01T00:00:00Z",
    )
    assert set(trait_context(point)) == set(KNOWN_ATTRIBUTES)


# --- the table hash -----------------------------------------------------------


def _shipped_text() -> str:
    from itest.core.declarations import traits as traits_module

    return (
        traits_module.resources.files(traits_module.TABLE_PACKAGE)
        .joinpath(traits_module.TABLE_RESOURCE)
        .read_text(encoding="utf-8")
    )


def test_the_table_hash_is_twelve_hex_characters() -> None:
    digest = trait_table_hash()
    assert len(digest) == 12
    assert int(digest, 16) >= 0
    assert digest == trait_table_hash(load_traits())


def test_the_table_hash_moves_when_one_line_changes(tmp_path: Path) -> None:
    text = _shipped_text()
    edited = text.replace("applies_when: egress != none", "applies_when: always")
    assert edited != text
    before = trait_table_hash(load_traits(_table(tmp_path, text)))
    after = trait_table_hash(load_traits(_table(tmp_path, edited)))
    assert before != after


def test_the_table_hash_ignores_whitespace_only_edits(tmp_path: Path) -> None:
    """The parsed structure is hashed, not the bytes: re-indenting, blank
    lines, trailing spaces, comments and spacing inside an expression are not
    a table change, and must not read as one on every tool."""
    text = _shipped_text()
    document = yaml.safe_load(text)
    edited = set()
    for row in document["traits"]:
        before = row["applies_when"]
        if row["id"] == "blast.destructive_gating":
            row["applies_when"] = "  mutation==destructive  "
        if row["id"] == "blast.audit":
            row["applies_when"] = row["applies_when"].replace(", ", ",")
        if row["applies_when"] != before:
            edited.add(row["id"])
    # Each respacing really changed its row's text; otherwise the test proves
    # nothing about canonicalization.
    assert edited == {"blast.destructive_gating", "blast.audit"}
    # Same content, entirely different bytes: flow style, 4-space indent.
    reflowed = "# a new comment\n\n" + yaml.safe_dump(
        document, sort_keys=False, default_flow_style=True, indent=4
    )
    assert reflowed != text
    assert trait_table_hash(load_traits(_table(tmp_path, reflowed))) == (
        trait_table_hash(load_traits(_table(tmp_path, text)))
    )


# --- the two derivations the table depends on ---------------------------------


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({}, False),
        ({"type": "object", "properties": {}}, False),
        ({"properties": {"id": {"type": "string"}}}, True),
        ({"properties": {"n": {"type": "integer"}}}, False),
        ({"properties": {"id": {"type": "string", "enum": ["a", "b"]}}}, False),
        ({"properties": {"id": {"type": "string", "pattern": "^r-"}}}, False),
        ({"properties": {"id": {"type": "string", "maxLength": 8}}}, False),
        ({"properties": {"id": {"type": ["string", "null"]}}}, True),
        # One unconstrained string among constrained ones is still a way in.
        (
            {
                "properties": {
                    "id": {"type": "string", "pattern": "^r-"},
                    "q": {"type": "string"},
                }
            },
            True,
        ),
    ],
)
def test_has_free_form_input(schema: dict, expected: bool) -> None:
    assert has_free_form_input(schema) is expected


def test_a_declaration_fills_a_gap_detection_left() -> None:
    """The third provenance. The server declared no annotations and the name
    suggested nothing, so `unknown` is an absence of detection rather than a
    disagreement — and a declared class stands, saying so in its provenance."""
    assert resolve_mutation("s", "frobnicate", "write", "unknown", "unknown") == (
        "write",
        "declared",
    )


def test_detection_wins_when_the_declaration_says_detect() -> None:
    assert resolve_mutation("s", "t", "detect", "destructive", "annotation") == (
        "destructive",
        "detected",
    )


def test_agreement_is_recorded_as_confirmed() -> None:
    assert resolve_mutation("s", "t", "read", "read", "annotation") == (
        "read",
        "confirmed",
    )
