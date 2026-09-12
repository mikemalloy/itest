"""The applies-when table, and the tiny language its one interesting column is in.

Which checks a tool gets is **data**: ``itest/traits/traits.yaml`` holds one row
per trait, and the engine has no hardcoded copy of its rules. That matters
because the table is the part of this design most likely to be argued with — a
project will want a check withheld, or a new one applied to more tools — and an
argument about policy should be settled in a reviewed YAML diff, not in a patch
to the generator.

``applies_when`` is deliberately the smallest language that expresses the rows
that ship:

===========================  ==============================================
``always``                   every tool
``<field>``                  the field is truthy
``<field> present``          the field is not null
``<field> == <value>``       equality
``<field> != <value>``       inequality
``<field> in [<a>, <b>]``    membership
``<A> and <B>``              conjunction
``<A> or <B>``               disjunction; ``and`` binds tighter, so
                             ``a and b or c`` is ``(a and b) or c``. No
                             parentheses and no ``not``.
===========================  ==============================================

Two refusals are on purpose. An **unknown field** is an error rather than a
false clause — a typo in the table would otherwise delete a check silently, and
a check nobody notices is missing is worse than no table at all. An
**unparseable clause** is an error for the same reason. Both name the trait and
the expression, and both are raised when the table is *loaded*, not when a sync
first reaches the row.

Every row also says **who runs the check** (``kind``): ``engine`` traits are run
by the engine straight from the manifest and have no per-tool file; ``generated``
traits get a thin per-tool binding stub, because they need a fact only a human
can supply (a second tenant's fixture, say). And the table as a whole has a
**hash** (:func:`trait_table_hash`) over its parsed content, which the manifest
records so a sync can say when the table itself changed.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib import resources
from pathlib import Path
from typing import Any, Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field

from itest.core.declarations.schema import NO_TRAITS
from itest.core.environments import VALID_TIERS
from itest.core.manifest import IntegrationPoint, Tier
from itest.traits.ids import migrate_trait_id

#: Where the shipped table lives: a resource of the ``itest.traits`` package,
#: read through :mod:`importlib.resources` and never by a path relative to this
#: file, so an installed wheel reads exactly what a source checkout reads.
TABLE_PACKAGE = "itest.traits"
TABLE_RESOURCE = "traits.yaml"

#: The only table version this build understands.
TABLE_VERSION = 1

#: Who runs a trait's check. ``engine``: the engine, from the manifest, with no
#: per-tool code. ``generated``: a thin per-tool stub bound to human fixtures.
TRAIT_KINDS = ("engine", "generated")

#: The attributes an ``applies_when`` may name — exactly the keys
#: :func:`trait_context` supplies (a test pins the two together). Checked at
#: load, so a typo refuses the table outright.
KNOWN_ATTRIBUTES = (
    "mutation",
    "egress",
    "approval",
    "active",
    "has_free_form_input",
    "auth.second_tenant_env",
    "audit.sink",
    "identity.runs_as",
    "transport.kind",
    "auth.enforced_over_stdio",
)

#: A trait id: ``<family>.<slug>``, lower case. The family half must be the
#: row's own family, so an id says where it belongs.
_TRAIT_ID = re.compile(r"^(?P<family>[a-z][a-z0-9_]*)\.[a-z][a-z0-9_]*$")

#: A display code: letters, a hyphen, a number (``AUTH-1``). Never an identity.
_TRAIT_CODE = re.compile(r"^[A-Z]+-[0-9]+$")

#: The published id families a ``standards`` entry may come from, with what
#: each looks like. A typo in one is an error; an empty list is fine.
STANDARDS_PREFIXES = (
    "ASI (OWASP Top 10 for Agentic Applications, e.g. ASI03)",
    "LLM (OWASP Top 10 for LLM Applications, e.g. LLM02)",
    "semgrep- (Semgrep MCP cheatsheet tab and row, e.g. semgrep-server-4)",
    "CWE- (e.g. CWE-862)",
    "ACS- (OWASP Agent Control Standard, e.g. ACS-AgBOM-mutation)",
)
_STANDARD = re.compile(
    r"^(ASI[0-9]{2}|LLM[0-9]{2}|semgrep-[a-z]+-[0-9]+|CWE-[0-9]+"
    r"|ACS-[A-Za-z0-9][A-Za-z0-9-]*)$"
)

_IN_CLAUSE = re.compile(r"^(?P<field>[\w.]+)\s+in\s+\[(?P<items>[^\]]*)\]$")
_COMPARISON = re.compile(r"^(?P<field>[\w.]+)\s*(?P<op>==|!=)\s*(?P<value>.+)$")
_PRESENT = re.compile(r"^(?P<field>[\w.]+)\s+present$")
_FIELD = re.compile(r"^[\w.]+$")


class TraitTableError(Exception):
    """A trait table that cannot be used, or an expression that cannot be read."""


class Trait(BaseModel):
    """One row of the table: a check, when it applies, and who implements it."""

    model_config = ConfigDict(extra="forbid")

    #: ``<family>.<slug>``: the trait's identity everywhere.
    id: str
    #: A short display label for narrow columns (``AUTH-1``). Never an identity.
    code: str
    family: str
    name: str
    applies_when: str
    #: The published ids this trait answers (``ASI03``, ``semgrep-server-4``).
    #: Empty until there is a confident mapping — never an invented id.
    standards: list[str] = Field(default_factory=list)
    #: What the generated stub is registered as, and what the environment policy
    #: gates on. An ``active`` trait's stubs live in their own file.
    tier: Tier
    #: ``engine`` (run from the manifest, no per-tool file) or ``generated``
    #: (a thin per-tool binding stub). See :data:`TRAIT_KINDS`.
    kind: Literal["engine", "generated"]
    #: The recipe (in the bundled skill) that says how to implement the check.
    recipe: str


class TraitTable(BaseModel):
    """The whole table: a version, the family names, and the rows."""

    model_config = ConfigDict(extra="forbid")

    version: int = TABLE_VERSION
    families: dict[str, str] = Field(default_factory=dict)
    traits: list[Trait] = Field(default_factory=list)

    def get(self, trait_id: str) -> Trait | None:
        """The row for ``trait_id``; an old AN-style id finds its renamed row."""
        trait_id = migrate_trait_id(trait_id)
        for trait in self.traits:
            if trait.id == trait_id:
                return trait
        return None

    @property
    def ids(self) -> list[str]:
        return [trait.id for trait in self.traits]


def load_traits(path: Path | None = None) -> TraitTable:
    """Load the applies-when table. Defaults to the one shipped with the engine."""
    if path is None:
        path = resources.files(TABLE_PACKAGE).joinpath(TABLE_RESOURCE)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise TraitTableError(f"no trait table at {path}") from None
    except OSError as exc:  # a directory, no read permission
        raise TraitTableError(
            f"trait table {path} could not be read: {exc.strerror}"
        ) from exc
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
    _check_rows(path, raw)

    try:
        table = TraitTable.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError, rendered with the path
        raise TraitTableError(f"{path} is not a valid trait table: {exc}") from None

    if not table.traits:
        raise TraitTableError(f"{path} defines no traits.")
    seen: set[str] = set()
    codes: dict[str, str] = {}
    for trait in table.traits:
        if trait.id in seen:
            raise TraitTableError(f"{path} defines trait {trait.id!r} twice.")
        seen.add(trait.id)
        if trait.code in codes:
            raise TraitTableError(
                f"{path}: traits {codes[trait.code]} and {trait.id} both have code "
                f"{trait.code!r}. A code is a display label, but two rows sharing "
                "one would read as the same check."
            )
        codes[trait.code] = trait.id
        try:
            for field in referenced_attributes(trait.applies_when):
                if field not in KNOWN_ATTRIBUTES:
                    raise TraitTableError(
                        f"applies_when {trait.applies_when!r} names an unknown "
                        f"attribute {field!r}. Known attributes: "
                        f"{', '.join(sorted(KNOWN_ATTRIBUTES))}."
                    )
        except TraitTableError as exc:
            raise TraitTableError(f"{path}: trait {trait.id}: {exc}") from None
    return table


def _check_rows(path: object, raw: dict) -> None:
    """The refusals that name a trait, before pydantic sees the rows.

    Pydantic would refuse most of these too, but with a location like
    ``traits.3.kind``; a reader fixing the table wants the trait id, the bad
    value and the values that would work.
    """
    families = raw.get("families") or {}
    rows = raw.get("traits") or []
    if not isinstance(rows, list):
        return  # pydantic's refusal is precise enough for a non-list
    for row in rows:
        if not isinstance(row, dict):
            continue
        trait_id = row.get("id", "(no id)")
        match = _TRAIT_ID.match(str(trait_id))
        if "id" in row and match is None:
            raise TraitTableError(
                f"{path}: trait id {trait_id!r} is not <family>.<slug> in lower "
                "case (e.g. authority.anonymous). An old AN-style id (A1, B2, ...) "
                "belongs in a manifest being migrated, never in the table."
            )
        if match and row.get("family") in families and match["family"] != row["family"]:
            raise TraitTableError(
                f"{path}: trait {trait_id} is in family {row['family']!r}, but its "
                f"id starts with {match['family']!r}. An id starts with its "
                "family's id."
            )
        if "code" in row and not _TRAIT_CODE.match(str(row["code"])):
            raise TraitTableError(
                f"{path}: trait {trait_id} has code {row['code']!r}. A code is "
                "upper-case letters, a hyphen and a number, e.g. AUTH-1."
            )
        standards = row.get("standards") or []
        if isinstance(standards, list):
            for entry in standards:
                if not isinstance(entry, str) or not _STANDARD.match(entry):
                    raise TraitTableError(
                        f"{path}: trait {trait_id} names standard {entry!r}, which "
                        "is not a published id this build recognises. Known "
                        f"prefixes: {'; '.join(STANDARDS_PREFIXES)}. Leave the "
                        "list empty until a mapping is confident."
                    )
        if "kind" in row and row["kind"] not in TRAIT_KINDS:
            raise TraitTableError(
                f"{path}: trait {trait_id} has kind {row['kind']!r}. A kind is "
                f"one of: {', '.join(TRAIT_KINDS)}."
            )
        if "tier" in row and row["tier"] not in VALID_TIERS:
            raise TraitTableError(
                f"{path}: trait {trait_id} has tier {row['tier']!r}, which the "
                f"environment policy does not define. Tiers: "
                f"{', '.join(VALID_TIERS)}."
            )
        if "family" in row and row["family"] not in families:
            raise TraitTableError(
                f"{path}: trait {trait_id} is in family {row['family']!r}, which "
                f"the table's families do not name. Families: "
                f"{', '.join(sorted(families)) or '(none)'}."
            )


def trait_table_hash(table: TraitTable | None = None) -> str:
    """A 12-character sha256 of the table's canonical content.

    Canonical means *parsed*: the rows as the model holds them, every
    ``applies_when`` re-rendered from its parse, and the whole dumped as sorted
    JSON. Re-indenting the YAML, adding a comment or respacing an expression is
    not a table change and does not move the hash; changing what any row says
    does. The manifest records it, so a sync can say the table changed.
    """
    table = table if table is not None else load_traits()
    document = table.model_dump(mode="json")
    for row in document["traits"]:
        row["applies_when"] = canonical_expression(row["applies_when"])
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# --- the expression language ---------------------------------------------------


def _literal(text: str) -> Any:
    """Parse a bare literal: ``none``, ``true``/``false``, an integer, or a word.

    A literal is one token. Refusing a value with whitespace in it is what stops
    ``a == b c`` from parsing as "a equals the string 'b c'" and evaluating,
    silently, to False.
    """
    token = text.strip().strip("'\"")
    if any(char.isspace() for char in token):
        raise TraitTableError(
            f"applies_when literal {token!r} is not a single value. The only "
            "connectives are 'and' and 'or', and a literal may not contain a "
            "space."
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


class _Clause(NamedTuple):
    """One parsed clause. ``op`` is always | truthy | present | == | != | in."""

    op: str
    field: str | None = None
    value: Any = None


def _parse_clause(clause: str, expression: str) -> _Clause:
    text = clause.strip()
    if text == "always":
        return _Clause("always")

    match = _PRESENT.match(text)
    if match:
        return _Clause("present", match["field"])

    match = _IN_CLAUSE.match(text)
    if match:
        values = [_literal(item) for item in match["items"].split(",") if item.strip()]
        return _Clause("in", match["field"], tuple(values))

    match = _COMPARISON.match(text)
    if match:
        return _Clause(match["op"], match["field"], _literal(match["value"]))

    if _FIELD.match(text):
        return _Clause("truthy", text)

    raise TraitTableError(
        f"applies_when {expression!r} has a clause this build cannot read: "
        f"{text!r}. The grammar is: always | <field> | <field> present | "
        "<field> == <value> | <field> != <value> | <field> in [a, b], joined "
        "by 'and', with 'or' between groups ('and' binds tighter)."
    )


#: A parsed expression: a disjunction of conjunctions. ``a and b or c`` is
#: ``[[a, b], [c]]`` — ``and`` binds tighter, and there are no parentheses.
Disjunction = list[list[_Clause]]

_OR = re.compile(r"\s+or\s+")
_AND = re.compile(r"\s+and\s+")


def parse(expression: str) -> Disjunction:
    """Parse ``expression`` into its clauses, or raise :class:`TraitTableError`.

    Parsing needs no context, which is what lets the loader refuse an
    unreadable row before any tool is evaluated against it. The result is a
    list of ``and``-groups joined by ``or``: the expression holds when any
    group holds, and a group holds when every clause in it does.
    """
    text = (expression or "").strip()
    if not text:
        raise TraitTableError("an empty applies_when matches nothing; write 'always'.")
    groups = _OR.split(text)
    parsed: Disjunction = []
    for group in groups:
        clauses = _AND.split(group)
        if any(clause.count("[") != clause.count("]") for clause in clauses):
            raise TraitTableError(
                f"applies_when {expression!r} splits a bracketed list across an "
                "'and' or an 'or'. A list literal may not contain either word."
            )
        parsed.append([_parse_clause(clause, text) for clause in clauses])
    return parsed


def referenced_attributes(expression: str) -> list[str]:
    """Every attribute ``expression`` names, in order."""
    return [
        clause.field for group in parse(expression) for clause in group if clause.field
    ]


def _render_literal(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_clause(clause: _Clause) -> str:
    if clause.op == "always":
        return "always"
    if clause.op == "truthy":
        return str(clause.field)
    if clause.op == "present":
        return f"{clause.field} present"
    if clause.op == "in":
        items = ", ".join(_render_literal(v) for v in clause.value)
        return f"{clause.field} in [{items}]"
    return f"{clause.field} {clause.op} {_render_literal(clause.value)}"


def canonical_expression(expression: str) -> str:
    """``expression`` re-rendered from its parse: one spelling per meaning."""
    return " or ".join(
        " and ".join(_render_clause(clause) for clause in group)
        for group in parse(expression)
    )


def _holds(clause: _Clause, context: dict[str, Any], expression: str) -> bool:
    if clause.op == "always":
        return True
    value = _lookup(clause.field, context, expression)
    if clause.op == "present":
        return value is not None
    if clause.op == "truthy":
        return bool(value)
    if clause.op == "in":
        return value in clause.value
    return value == clause.value if clause.op == "==" else value != clause.value


def evaluate(expression: str, context: dict[str, Any]) -> bool:
    """True when any ``or``-joined group of ``expression`` holds, a group
    holding when every ``and``-joined clause in it does.

    Every clause is evaluated — no short circuit — so an unknown attribute
    anywhere in the expression is an error, not a clause that happened never
    to be reached.
    """
    text = (expression or "").strip()
    outcomes = [
        [_holds(clause, context, text) for clause in group]
        for group in parse(expression)
    ]
    return any(all(group) for group in outcomes)


def applicable(table: TraitTable, context: dict[str, Any]) -> list[Trait]:
    """The traits that apply to one tool, in table order.

    Table order is deliberate: it is the order stubs are generated in, so a
    regenerated file reads the same way every time.
    """
    return [trait for trait in table.traits if evaluate(trait.applies_when, context)]


# --- deciding one tool ------------------------------------------------------------


def trait_context(point: IntegrationPoint) -> dict[str, Any]:
    """The attribute names the applies-when table evaluates against.

    Built from the point alone: sync reads the manifest and the changeset, never
    the declaration, so everything the table asks about has to be on the point.
    Exactly :data:`KNOWN_ATTRIBUTES` (a test pins the two together). Lives here,
    beside the table, rather than with the probe-backed point builder, so that
    ``itest traits --for`` can decide a tool without importing a transport.
    """
    attributes = point.attributes
    return {
        "mutation": attributes.get("mutation"),
        "egress": attributes.get("egress"),
        "approval": attributes.get("approval"),
        "active": attributes.get("active"),
        "has_free_form_input": attributes.get("has_free_form_input"),
        "auth.second_tenant_env": attributes.get("second_tenant_env"),
        "audit.sink": attributes.get("audit_sink"),
        "identity.runs_as": attributes.get("runs_as"),
        # stdio | http, from the declaration's transport block.
        "transport.kind": attributes.get("transport_kind"),
        # A stdio server that checks a credential of its own; default false.
        "auth.enforced_over_stdio": bool(attributes.get("enforced_over_stdio")),
    }


class TraitDecision(NamedTuple):
    """Whether one trait applies to one tool, and what decided it."""

    trait: Trait
    applies: bool
    #: ``rule: <applies_when>``, ``declared traits: ...``, or the active-tier
    #: withholding. Printed by the plan and by ``itest traits --for``.
    reason: str


def trait_decisions(point: IntegrationPoint, table: TraitTable) -> list[TraitDecision]:
    """Every trait in the table, decided for one declared tool, in table order.

    The table decides, unless the declaration hand-picked a list — and a tool
    whose declaration withholds the active tier loses its active traits whichever
    way they were chosen. Withholding removes the check; it never leaves one
    behind that must not run. Raises :class:`TraitTableError` for a hand-picked
    trait the table does not define.
    """
    declared = point.attributes.get("traits")
    if declared:
        for trait_id in declared:
            if trait_id != NO_TRAITS and table.get(trait_id) is None:
                raise TraitTableError(
                    f"{point.source}/{point.target} is declared with trait "
                    f"{trait_id!r}, which the trait table does not define. "
                    f"Known traits: {', '.join(table.ids)}."
                )
    context = trait_context(point)
    withheld = point.attributes.get("active") is False

    decisions: list[TraitDecision] = []
    for trait in table.traits:
        if declared:
            applies = trait.id in declared
            reason = f"declared traits: {', '.join(declared)}"
        else:
            applies = evaluate(trait.applies_when, context)
            reason = f"rule: {trait.applies_when}"
        if applies and withheld and trait.tier == "active":
            applies = False
            reason = "active: false withholds active-tier checks"
        decisions.append(TraitDecision(trait, applies, reason))
    return decisions
