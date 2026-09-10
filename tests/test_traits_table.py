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

from pathlib import Path

import pytest
import yaml

from itest.core.declarations.tools import has_free_form_input, resolve_mutation
from itest.core.declarations.traits import (
    TraitTableError,
    applicable,
    evaluate,
    load_traits,
)

CONTEXT = {
    "mutation": "write",
    "egress": None,
    "approval": "none",
    "active": True,
    "has_free_form_input": True,
    "auth.second_tenant_env": "TENANT_B",
    "audit.sink": "stderr-json",
}


# --- the shipped table --------------------------------------------------------


def test_the_shipped_table_is_the_eleven_traits() -> None:
    table = load_traits()
    assert table.ids == [
        "A1",
        "A2",
        "B1",
        "B2",
        "B3",
        "B4",
        "C1",
        "C2",
        "D1",
        "D2",
        "D3",
    ]
    assert table.families == {
        "A": "Authority",
        "B": "Blast radius",
        "C": "Containment",
        "D": "Change",
    }
    # The tier is what the environment policy gates on, so it is policy too.
    assert {t.id for t in table.traits if t.tier == "active"} == {
        "A2",
        "B2",
        "B4",
        "C1",
        "C2",
    }
    assert {t.recipe for t in table.traits if t.family == "D"} == {"tool_provenance.md"}


def test_every_shipped_expression_evaluates() -> None:
    """No row of the committed table may be unreadable by the evaluator."""
    table = load_traits()
    for trait in table.traits:
        assert isinstance(evaluate(trait.applies_when, CONTEXT), bool)


def test_an_informational_tool_gets_only_the_always_traits() -> None:
    context = dict(CONTEXT, mutation="informational", has_free_form_input=False)
    assert [t.id for t in applicable(load_traits(), context)] == [
        "A1",
        "B1",
        "D1",
        "D2",
        "D3",
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
    path = tmp_path / "traits.yaml"
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_missing_table_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(tmp_path / "nope.yaml")
    assert "nope.yaml" in str(excinfo.value)


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
    row = {
        "id": "A1",
        "family": "A",
        "name": "refuses anonymous",
        "applies_when": "always",
        "tier": "readonly",
        "recipe": "tool_authn.md",
    }
    path = _table(tmp_path, {"version": 1, "traits": [row, dict(row)]})
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    assert "A1" in str(excinfo.value)


def test_an_unknown_key_in_a_row_is_refused(tmp_path: Path) -> None:
    path = _table(
        tmp_path,
        {
            "version": 1,
            "traits": [
                {
                    "id": "A1",
                    "family": "A",
                    "name": "x",
                    "applies_when": "always",
                    "tier": "readonly",
                    "recipe": "r.md",
                    "danger": True,
                }
            ],
        },
    )
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(path)
    assert "danger" in str(excinfo.value)


def test_an_unknown_tier_is_refused(tmp_path: Path) -> None:
    path = _table(
        tmp_path,
        {
            "version": 1,
            "traits": [
                {
                    "id": "A1",
                    "family": "A",
                    "name": "x",
                    "applies_when": "always",
                    "tier": "whenever",
                    "recipe": "r.md",
                }
            ],
        },
    )
    with pytest.raises(TraitTableError):
        load_traits(path)


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
