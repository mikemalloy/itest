"""The applies-when table, and the tiny language its one interesting column is in.

Which checks a tool gets is **data**: ``itest/traits/traits.yaml`` holds one row
per trait, and the engine has no hardcoded copy of its rules. That matters
because the table is the part of this design most likely to be argued with — a
project will want a check withheld, or a new one applied to more tools — and an
argument about policy should be settled in a reviewed YAML diff, not in a patch
to the generator.

``applies_when`` is deliberately the smallest language that expresses the eleven
rows that ship:

===========================  ==============================================
``always``                   every tool
``<field>``                  the field is truthy
``<field> present``          the field is not null
``<field> == <value>``       equality
``<field> != <value>``       inequality
``<field> in [<a>, <b>]``    membership
``<A> and <B>``              conjunction (no ``or``, and no precedence to get
                             wrong)
===========================  ==============================================

Two refusals are on purpose. An **unknown field** is an error rather than a
false clause — a typo in the table would otherwise delete a check silently, and
a check nobody notices is missing is worse than no table at all. An
**unparseable clause** is an error for the same reason. Both name the trait and
the expression.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from itest.core.manifest import Tier

#: The shipped table. Overridable only by passing a path to :func:`load_traits`
#: (which is how the test that proves the table is data edits one line of it).
TRAITS_PATH = Path(__file__).resolve().parents[2] / "traits" / "traits.yaml"

#: The only table version this build understands.
TABLE_VERSION = 1

_IN_CLAUSE = re.compile(r"^(?P<field>[\w.]+)\s+in\s+\[(?P<items>[^\]]*)\]$")
_COMPARISON = re.compile(r"^(?P<field>[\w.]+)\s*(?P<op>==|!=)\s*(?P<value>.+)$")
_PRESENT = re.compile(r"^(?P<field>[\w.]+)\s+present$")
_FIELD = re.compile(r"^[\w.]+$")


class TraitTableError(Exception):
    """A trait table that cannot be used, or an expression that cannot be read."""


class Trait(BaseModel):
    """One row of the table: a check, when it applies, and who implements it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    family: str
    name: str
    applies_when: str
    #: What the generated stub is registered as, and what the environment policy
    #: gates on. An ``active`` trait's stubs live in their own file.
    tier: Tier
    #: The recipe (in the bundled skill) that says how to implement the check.
    recipe: str


class TraitTable(BaseModel):
    """The whole table: a version, the family names, and the rows."""

    model_config = ConfigDict(extra="forbid")

    version: int = TABLE_VERSION
    families: dict[str, str] = Field(default_factory=dict)
    traits: list[Trait] = Field(default_factory=list)

    def get(self, trait_id: str) -> Trait | None:
        for trait in self.traits:
            if trait.id == trait_id:
                return trait
        return None

    @property
    def ids(self) -> list[str]:
        return [trait.id for trait in self.traits]


def load_traits(path: Path | None = None) -> TraitTable:
    """Load the applies-when table. Defaults to the one shipped with the engine."""
    path = Path(path) if path is not None else TRAITS_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise TraitTableError(f"no trait table at {path}") from None
    except yaml.YAMLError as exc:
        raise TraitTableError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise TraitTableError(f"{path} must be a mapping with a 'traits' list.")
    version = raw.get("version")
    if version != TABLE_VERSION:
        raise TraitTableError(
            f"{path} declares version {version!r}, but this build understands "
            f"only version {TABLE_VERSION}."
        )

    try:
        table = TraitTable.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError, rendered with the path
        raise TraitTableError(f"{path} is not a valid trait table: {exc}") from None

    if not table.traits:
        raise TraitTableError(f"{path} defines no traits.")
    seen: set[str] = set()
    for trait in table.traits:
        if trait.id in seen:
            raise TraitTableError(f"{path} defines trait {trait.id!r} twice.")
        seen.add(trait.id)
    return table


# --- the expression language ---------------------------------------------------


def _literal(text: str) -> Any:
    """Parse a bare literal: ``none``, ``true``/``false``, an integer, or a word.

    A literal is one token. Refusing a value with whitespace in it is what stops
    ``a == b or c == d`` from parsing as "a equals the string 'b or c == d'" and
    evaluating, silently, to False — the only connective is ``and``.
    """
    token = text.strip().strip("'\"")
    if any(char.isspace() for char in token):
        raise TraitTableError(
            f"applies_when literal {token!r} is not a single value. The only "
            "connective is 'and'; there is no 'or', and a literal may not "
            "contain a space."
        )
    lowered = token.lower()
    if lowered in ("none", "null"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(token)
    except ValueError:
        return token


def _lookup(field: str, context: dict[str, Any], expression: str) -> Any:
    """Read a field from the context, refusing one the context does not define.

    The refusal is the point: a mistyped field that evaluated to ``False`` would
    silently withhold a check, and nothing downstream would ever say so.
    """
    if field not in context:
        known = ", ".join(sorted(context))
        raise TraitTableError(
            f"applies_when {expression!r} names an unknown attribute {field!r}. "
            f"Known attributes: {known}."
        )
    return context[field]


def _clause(clause: str, context: dict[str, Any], expression: str) -> bool:
    text = clause.strip()
    if text == "always":
        return True

    match = _PRESENT.match(text)
    if match:
        return _lookup(match["field"], context, expression) is not None

    match = _IN_CLAUSE.match(text)
    if match:
        values = [_literal(item) for item in match["items"].split(",") if item.strip()]
        return _lookup(match["field"], context, expression) in values

    match = _COMPARISON.match(text)
    if match:
        value = _lookup(match["field"], context, expression)
        wanted = _literal(match["value"])
        return value == wanted if match["op"] == "==" else value != wanted

    if _FIELD.match(text):
        return bool(_lookup(text, context, expression))

    raise TraitTableError(
        f"applies_when {expression!r} has a clause this build cannot read: "
        f"{text!r}. The grammar is: always | <field> | <field> present | "
        "<field> == <value> | <field> != <value> | <field> in [a, b], joined "
        "by 'and'."
    )


def evaluate(expression: str, context: dict[str, Any]) -> bool:
    """True when every ``and``-joined clause of ``expression`` holds."""
    text = (expression or "").strip()
    if not text:
        raise TraitTableError("an empty applies_when matches nothing; write 'always'.")
    clauses = re.split(r"\s+and\s+", text)
    if any(clause.count("[") != clause.count("]") for clause in clauses):
        raise TraitTableError(
            f"applies_when {expression!r} splits a bracketed list across an "
            "'and'. A list literal may not contain the word 'and'."
        )
    return all(_clause(clause, context, text) for clause in clauses)


def applicable(table: TraitTable, context: dict[str, Any]) -> list[Trait]:
    """The traits that apply to one tool, in table order.

    Table order is deliberate: it is the order stubs are generated in, so a
    regenerated file reads the same way every time.
    """
    return [trait for trait in table.traits if evaluate(trait.applies_when, context)]
