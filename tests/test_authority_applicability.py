"""authority.anonymous applies only where "anonymous" means something.

Over stdio the process boundary IS the authentication boundary: whoever can
spawn the subprocess is, by definition, authorised, and there is no anonymous
caller to refuse. A check that reported "anonymous call succeeded" on such a
server would be noise — the kind that gets a tool switched off. So the trait
applies to a network transport, or to a stdio server whose owner declares that
it checks a credential of its own (``auth.enforced_over_stdio``). The rule is a
row of ``traits.yaml``, not a conditional anywhere in the engine.
"""

from __future__ import annotations

from typing import Any

from itest.core.declarations.schema import Declaration
from itest.core.declarations.tools import build_points
from itest.core.declarations.traits import load_traits, trait_decisions
from itest.probes.mcp import ToolInfo

RULE = "transport.kind == http or auth.enforced_over_stdio"

TOOLS = [
    ToolInfo(
        name="get_guide",
        description="guide",
        input_schema={},
        output_schema=None,
        annotations={},
        schema_hash="a" * 12,
        description_hash="b" * 12,
    ),
    ToolInfo(
        name="delete_record",
        description="delete",
        input_schema={"properties": {"id": {"type": "string"}}, "required": ["id"]},
        output_schema=None,
        annotations={"readOnlyHint": False, "destructiveHint": True},
        schema_hash="c" * 12,
        description_hash="d" * 12,
    ),
]


def _declaration(**changes: Any) -> Declaration:
    document: dict[str, Any] = {
        "server": "srv",
        "transport": {"kind": "stdio", "command": ["python", "server.py"]},
        "auth": {"scheme": "bearer", "credential_env": "SRV_TOKEN"},
        "sentinels": {"nonexistent_id": "nope-0000"},
    }
    for key, value in changes.items():
        document[key] = value
    return Declaration.model_validate(document)


def _anonymous(declaration: Declaration) -> dict[str, tuple[bool, str]]:
    table = load_traits()
    out = {}
    for point in build_points(declaration, TOOLS, table):
        decision = next(
            d
            for d in trait_decisions(point, table)
            if d.trait.id == "authority.anonymous"
        )
        out[point.target] = (decision.applies, decision.reason)
    return out


def test_the_rule_is_data_in_the_table() -> None:
    assert load_traits().get("authority.anonymous").applies_when == RULE


def test_a_stdio_server_gets_no_anonymous_check_on_any_tool() -> None:
    decisions = _anonymous(_declaration())
    assert set(decisions) == {"get_guide", "delete_record"}
    for tool, (applies, reason) in decisions.items():
        assert applies is False, tool
        assert reason == f"rule: {RULE}"


def test_a_stdio_server_that_declares_its_own_enforcement_gets_it_everywhere() -> None:
    declaration = _declaration(
        auth={
            "scheme": "bearer",
            "credential_env": "SRV_TOKEN",
            "enforced_over_stdio": True,
        }
    )
    decisions = _anonymous(declaration)
    assert {applies for applies, _ in decisions.values()} == {True}


def test_an_http_server_gets_it_everywhere() -> None:
    declaration = _declaration(transport={"kind": "http", "url_env": "SRV_URL"})
    decisions = _anonymous(declaration)
    assert {applies for applies, _ in decisions.values()} == {True}


def test_the_points_carry_the_two_facts() -> None:
    (point, _) = build_points(_declaration(), TOOLS, load_traits())
    assert point.attributes["transport_kind"] == "stdio"
    assert point.attributes["enforced_over_stdio"] is False
    (point, _) = build_points(
        _declaration(transport={"kind": "http", "url_env": "SRV_URL"}),
        TOOLS,
        load_traits(),
    )
    assert point.attributes["transport_kind"] == "http"


def test_enforced_over_stdio_defaults_to_false() -> None:
    """Absence grants nothing: a stdio server is taken to trust the spawn
    unless its owner says otherwise."""
    assert _declaration().auth.enforced_over_stdio is False
