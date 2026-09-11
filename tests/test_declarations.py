"""The declaration file: `.itest/tools/<server>.yaml`, its schema and its loader.

A declaration is how an MCP server becomes ITest points. It holds **facts** —
where the server is, which env var holds its token, what a non-existent id looks
like — and nothing else. Three rules are load-bearing and each is pinned here:

1. **The developer never declares the dangerous field.** ``defaults.mutation``
   accepts ``detect`` (the default) and a real class, but a declared class is
   only ever *cross-checked* against what the live listing says. That check is in
   tests/test_declarations_plan_sync.py; what this file pins is that the schema
   accepts the fact and never invents one.
2. **No URL and no credential is ever literal.** ``transport.url_env`` and
   ``auth.credential_env`` name environment variables. A URL written into the
   file is a validation error, and no error message this module produces ever
   contains the resolved URL — a declaration is committed, and errors are pasted
   into issues.
3. **A declaration cannot grant a tier the committed policy withholds.**
   ``environments.active_allowed_in`` must name environments that
   ``.itest/environments.yaml`` already permits ``active`` in. Absence of a
   policy is not permission.

Unknown keys are errors everywhere: a typo in a declaration would otherwise be a
fact silently not stated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from itest.core.declarations import (
    Declaration,
    DeclarationError,
    declaration_path,
    load_declarations,
    missing_url_message,
    resolve_url,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (
    REPO_ROOT / "examples" / "reference-mcp" / ".itest" / "tools" / "reference-mcp.yaml"
)

POLICY = """\
version: 1
environments:
  staging:
    tiers: [static, readonly, active]
  prod:
    tiers: [static, readonly]
"""


def _write(base_dir: Path, name: str, document: dict | str) -> Path:
    """Write a declaration into ``base_dir/.itest/tools/<name>.yaml``."""
    path = base_dir / ".itest" / "tools" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    path.write_text(text, encoding="utf-8")
    return path


def _policy(base_dir: Path, text: str = POLICY) -> None:
    path = base_dir / ".itest" / "environments.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout holding the real example declaration and a policy for it."""
    _write(tmp_path, "reference-mcp", EXAMPLE.read_text(encoding="utf-8"))
    _policy(tmp_path)
    return tmp_path


def _minimal(**overrides: object) -> dict:
    """The smallest declaration that validates, for the error-path tests."""
    document: dict = {
        "server": "reference-mcp",
        "transport": {"kind": "stdio", "command": ["python", "server.py"]},
        "sentinels": {"nonexistent_id": "sentinel-0000"},
    }
    document.update(overrides)
    return document


# --- round-trip ---------------------------------------------------------------


def test_the_example_declaration_round_trips(checkout: Path) -> None:
    """The committed example is the format's worked answer: it must load, and
    every field must arrive as the fact it states."""
    (declaration,) = load_declarations(checkout)

    assert declaration.server == "reference-mcp"
    assert declaration.transport.kind == "stdio"
    # Launched in the project directory, so the server is the file beside it.
    assert declaration.transport.command == ["python", "server.py"]
    assert declaration.transport.url_env == "REFERENCE_MCP_URL"

    assert declaration.auth.scheme == "bearer"
    assert declaration.auth.credential_env == "REFERENCE_MCP_TOKEN"
    assert declaration.auth.second_tenant_env == "REFERENCE_MCP_TOKEN_TENANT_B"

    assert declaration.tenancy.scoped_by == "credential"
    assert declaration.identity.runs_as == "service"
    assert declaration.identity.backing_role == "reference-mcp-service"
    assert declaration.audit.sink == "stderr-json"
    assert declaration.approval.destructive_requires == "none"

    assert declaration.sentinels.nonexistent_id == "sentinel-cannot-exist-0000"
    assert declaration.sentinels.readable_record == "r-1"
    assert declaration.environments.active_allowed_in == ["staging"]

    assert declaration.defaults.mutation == "detect"
    assert declaration.defaults.egress == "none"
    assert declaration.defaults.active is True

    assert set(declaration.tools) == {"delete_record", "enrich"}
    assert declaration.tools["delete_record"].approval == "confirm_param"
    # Detected, never declared.
    assert declaration.tools["delete_record"].mutation is None
    assert declaration.tools["enrich"].egress.to == "enrichment-provider.example"
    assert declaration.tools["enrich"].egress.data == ["email"]
    # The behavioural liar is deliberately not declared: nothing in the tool
    # listing disagrees with anything else, so there is nothing to state.
    assert "lookalike_read" not in declaration.tools


def test_the_example_declares_no_mutation_class_for_any_tool(checkout: Path) -> None:
    """Facts in, traits out. The example states approval, egress and sentinels —
    never what a tool *does*, which is detected."""
    (declaration,) = load_declarations(checkout)
    assert declaration.defaults.mutation == "detect"
    assert all(o.mutation is None for o in declaration.tools.values())


def test_no_tools_directory_means_no_declarations(tmp_path: Path) -> None:
    """A project that has never declared a server is exactly today's project."""
    assert load_declarations(tmp_path) == []


def test_declarations_load_in_a_stable_order(tmp_path: Path) -> None:
    _write(tmp_path, "zulu", _minimal(server="zulu"))
    _write(tmp_path, "alpha", _minimal(server="alpha"))
    assert [d.server for d in load_declarations(tmp_path)] == ["alpha", "zulu"]


def test_the_declaration_path_is_derived_from_the_server_name() -> None:
    assert declaration_path("reference-mcp") == ".itest/tools/reference-mcp.yaml"


# --- unknown keys are errors --------------------------------------------------


def test_an_unknown_top_level_key_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", _minimal(dangerous=True))
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "dangerous" in str(excinfo.value)


def test_an_unknown_nested_key_is_an_error(tmp_path: Path) -> None:
    """A typo must not read as a fact left unstated."""
    _write(tmp_path, "reference-mcp", _minimal(defaults={"mutations": "detect"}))
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "mutations" in str(excinfo.value)
    assert "defaults" in str(excinfo.value)


def test_an_unknown_key_in_a_tool_override_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(tools={"delete_record": {"apporval": "human"}}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "apporval" in str(excinfo.value)


# --- identity and file naming -------------------------------------------------


@pytest.mark.parametrize("name", ["Reference-MCP", "reference_mcp", "ref mcp", ""])
def test_a_server_name_outside_the_vocabulary_is_an_error(
    tmp_path: Path, name: str
) -> None:
    _write(tmp_path, "reference-mcp", _minimal(server=name))
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "server" in str(excinfo.value)


def test_the_file_name_must_be_the_server_name(tmp_path: Path) -> None:
    """The path is the address: `.itest/tools/<server>.yaml`. A mismatch would
    make two files claim one server, or one file claim a server nothing reaches."""
    _write(tmp_path, "other-name", _minimal(server="reference-mcp"))
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    message = str(excinfo.value)
    assert "other-name" in message and "reference-mcp" in message


# --- transport ----------------------------------------------------------------


def test_a_stdio_transport_needs_a_command(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", _minimal(transport={"kind": "stdio"}))
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "command" in str(excinfo.value)


def test_an_http_transport_needs_a_url_env(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", _minimal(transport={"kind": "http"}))
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "url_env" in str(excinfo.value)


def test_a_literal_url_is_refused_and_never_echoed(tmp_path: Path) -> None:
    """The rule credentials already follow, applied to the URL: the file names an
    environment variable. A file that carried the URL itself would put a hostname
    (often an internal one) into the repository and into every error about it."""
    secret_url = "https://internal-mcp.corp.example/mcp"
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(transport={"kind": "http", "url_env": secret_url}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    message = str(excinfo.value)
    assert "url_env" in message
    assert "environment variable" in message
    # The whole point: the value never travels with the complaint about it.
    assert secret_url not in message
    assert "internal-mcp.corp.example" not in message


def test_a_literal_credential_is_refused_and_never_echoed(tmp_path: Path) -> None:
    token = "sk-live-4b17f9-do-not-log"
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(auth={"scheme": "bearer", "credential_env": token}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert token not in str(excinfo.value)
    assert "credential_env" in str(excinfo.value)


# --- auth ---------------------------------------------------------------------


def test_bearer_without_a_credential_env_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", _minimal(auth={"scheme": "bearer"}))
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "credential_env" in str(excinfo.value)


def test_scheme_none_with_a_credential_env_is_an_error(tmp_path: Path) -> None:
    """One of the two is wrong, and the loader cannot know which."""
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(auth={"scheme": "none", "credential_env": "SOME_TOKEN"}),
    )
    with pytest.raises(DeclarationError):
        load_declarations(tmp_path)


def test_a_second_tenant_without_a_first_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(auth={"scheme": "none", "second_tenant_env": "TENANT_B"}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "second_tenant_env" in str(excinfo.value)


# --- sentinels, defaults, overrides -------------------------------------------


def test_a_declaration_without_a_sentinel_id_is_an_error(tmp_path: Path) -> None:
    """A mutating probe is proven by being admitted on an id that cannot exist.
    Without one there is nothing safe to call with."""
    document = _minimal()
    del document["sentinels"]
    _write(tmp_path, "reference-mcp", document)
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "sentinels" in str(excinfo.value)


def test_an_unknown_mutation_class_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", _minimal(defaults={"mutation": "dangerous"}))
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "mutation" in str(excinfo.value)


def test_a_declared_mutation_class_is_accepted_for_the_cross_check(
    tmp_path: Path,
) -> None:
    """Stating a class is allowed — it is how a developer says what they believe.
    It is never taken as the answer: plan cross-checks it against the live
    listing and refuses on a disagreement."""
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(tools={"delete_record": {"mutation": "destructive"}}),
    )
    (declaration,) = load_declarations(tmp_path)
    assert declaration.tools["delete_record"].mutation == "destructive"


def test_an_egress_edge_needs_a_destination(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(tools={"enrich": {"egress": {"data": ["email"]}}}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "to" in str(excinfo.value)


def test_egress_none_is_spelled_out(tmp_path: Path) -> None:
    """`none` is a value, not an absence: a tool may override a server-wide
    egress edge back to nothing, and that is a different statement from silence."""
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(
            defaults={"egress": {"to": "provider.example", "data": ["email"]}},
            tools={"fetch_record": {"egress": "none"}, "enrich": {}},
        ),
    )
    (declaration,) = load_declarations(tmp_path)
    assert declaration.defaults.egress.to == "provider.example"
    assert declaration.tools["fetch_record"].egress == "none"  # stated
    assert declaration.tools["enrich"].egress is None  # silent: inherits


# --- traits -------------------------------------------------------------------


def test_none_of_these_requires_notes(tmp_path: Path) -> None:
    """Saying a tool needs no checks is a claim someone must justify in writing."""
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(tools={"get_guide": {"traits": ["none-of-these"]}}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "notes" in str(excinfo.value)


def test_none_of_these_with_notes_is_accepted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(
            tools={
                "get_guide": {
                    "traits": ["none-of-these"],
                    "notes": "Static text; nothing to authorise or mutate.",
                }
            }
        ),
    )
    (declaration,) = load_declarations(tmp_path)
    assert declaration.tools["get_guide"].traits == ["none-of-these"]


def test_none_of_these_cannot_sit_beside_a_trait(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(
            tools={"get_guide": {"traits": ["none-of-these", "A1"], "notes": "why"}}
        ),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "none-of-these" in str(excinfo.value)


def test_a_trait_id_outside_the_shape_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path, "reference-mcp", _minimal(tools={"get_guide": {"traits": ["auth"]}})
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "traits" in str(excinfo.value)


# --- environments: a declaration cannot grant what the policy withholds -------


def test_active_allowed_in_must_name_an_environment_the_policy_permits(
    tmp_path: Path,
) -> None:
    _policy(tmp_path)
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(environments={"active_allowed_in": ["prod"]}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    message = str(excinfo.value)
    assert "prod" in message  # the offending environment is named
    assert "staging" in message  # and so is what would work


def test_active_allowed_in_naming_an_undefined_environment_is_an_error(
    tmp_path: Path,
) -> None:
    _policy(tmp_path)
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(environments={"active_allowed_in": ["qa"]}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "qa" in str(excinfo.value)


def test_active_allowed_in_without_a_policy_at_all_is_an_error(
    tmp_path: Path,
) -> None:
    """Absence is never permission: with no committed policy, no environment
    permits the active tier, so no declaration may claim one does."""
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(environments={"active_allowed_in": ["staging"]}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    message = str(excinfo.value)
    assert "staging" in message
    assert "environments.yaml" in message


def test_no_active_environments_needs_no_policy(tmp_path: Path) -> None:
    """A readonly-only declaration is fine in a project with no policy at all."""
    _write(tmp_path, "reference-mcp", _minimal())
    (declaration,) = load_declarations(tmp_path)
    assert declaration.environments.active_allowed_in == []


# --- malformed files ----------------------------------------------------------


def test_unparseable_yaml_names_the_file(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", "server: [unclosed\n")
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "reference-mcp.yaml" in str(excinfo.value)


def test_a_declaration_that_is_not_a_mapping_names_the_file(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", "- reference-mcp\n")
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "reference-mcp.yaml" in str(excinfo.value)


def test_an_empty_declaration_names_what_is_missing(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", "\n")
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    assert "server" in str(excinfo.value)


def test_a_yaml_error_names_the_key_and_line_never_the_value(tmp_path: Path) -> None:
    """PyYAML's own message quotes the offending source line, which is exactly
    where a pasted internal hostname would sit."""
    _write(
        tmp_path,
        "reference-mcp",
        "server: reference-mcp\n"
        "transport:\n"
        "  kind: http\n"
        "  url_env: https://mcp.internal-corp.example: 8443\n",
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    message = str(excinfo.value)
    assert "reference-mcp.yaml" in message
    assert "line 4" in message
    assert "url_env" in message
    assert "internal-corp" not in message
    assert "8443" not in message


def test_a_trait_listed_twice_for_one_tool_is_refused(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "reference-mcp",
        _minimal(tools={"enrich": {"traits": ["B3", "D1", "B3"]}}),
    )
    with pytest.raises(DeclarationError) as excinfo:
        load_declarations(tmp_path)
    message = str(excinfo.value)
    assert "tools.enrich.traits" in message
    assert "B3" in message
    assert "more than once" in message


def test_the_drift_attributes_are_defined_once() -> None:
    """The planner owns the set: it is the only reader, and it must not import
    the tools module (which loads the probe transport) to get it."""
    from itest.core import planner
    from itest.core.declarations import tools

    # The mutation class as detected — the resolved class and the annotations
    # it was read from — is drift too: an annotation flip is `changed`.
    assert planner.DRIFT_ATTRIBUTES == (
        "schema_hash",
        "description_hash",
        "mutation",
        "annotations_hash",
    )
    assert not hasattr(tools, "DRIFT_ATTRIBUTES")


# --- resolving the url, by name, without printing it --------------------------


@pytest.fixture(autouse=True)
def _no_ambient_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REFERENCE_MCP_URL", raising=False)


def test_an_unset_url_env_resolves_to_none(checkout: Path) -> None:
    (declaration,) = load_declarations(checkout)
    assert resolve_url(declaration, checkout) is None


def test_the_url_is_read_from_the_environment_by_name(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REFERENCE_MCP_URL", "https://mcp.example.test/mcp")
    (declaration,) = load_declarations(checkout)
    assert resolve_url(declaration, checkout) == "https://mcp.example.test/mcp"


def test_the_url_is_read_from_a_gitignored_env_file_too(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same path a credential takes: a name in the file, the value in
    `.itest/.env` or the shell."""
    # resolve_credential seeds os.environ with every key in the file. Swap in a
    # copy for this test so whatever it seeds is gone afterwards, all of it.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    (checkout / ".itest" / ".env").write_text(
        "REFERENCE_MCP_URL=https://from-the-env-file.example/mcp\n", encoding="utf-8"
    )
    (declaration,) = load_declarations(checkout)
    assert resolve_url(declaration, checkout) == (
        "https://from-the-env-file.example/mcp"
    )


def test_a_stdio_only_declaration_resolves_no_url(tmp_path: Path) -> None:
    _write(tmp_path, "reference-mcp", _minimal())
    (declaration,) = load_declarations(tmp_path)
    assert resolve_url(declaration, tmp_path) is None


def test_the_unreachable_message_names_the_variable_and_nothing_else(
    checkout: Path,
) -> None:
    (declaration,) = load_declarations(checkout)
    message = missing_url_message(declaration)
    assert message == "unreachable: REFERENCE_MCP_URL not set"


def test_a_declaration_is_a_plain_model(checkout: Path) -> None:
    """It is data: dumpable, comparable, and carrying nothing resolved."""
    (declaration,) = load_declarations(checkout)
    dumped = declaration.model_dump(mode="json")
    assert dumped["transport"]["url_env"] == "REFERENCE_MCP_URL"
    assert Declaration.model_validate(dumped) == declaration
