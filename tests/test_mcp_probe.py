"""The MCP probe transport, proven against the reference MCP server.

``itest/probes/mcp.py`` is the second probe transport: where the HTTP probe asks
"does the guard hold when I knock on this URL?", this one asks the same question
of an MCP server's tools. Its safety properties are BY CONSTRUCTION — enforced
in the module, not in recipe prose — and this file is where each one is pinned:

1. a credential is resolved by env-var NAME and never appears in a result, in a
   ``raw`` payload, or in a raised exception — proven with a distinctive token
   and a server that deliberately echoes it back;
2. every call has a timeout, and exceeding it is a distinct outcome from a
   refusal;
3. a tool whose mutation class is write or destructive is **refused before any
   connection is opened** unless the caller passes ``allow_mutating=True`` —
   proven by a stdio command that would leave a marker file on disk if it were
   ever launched;
4. an unauthenticated ``tools/call`` that a server admits on a mutating tool is
   CRITICAL, the same rule as the HTTP probe's unauthenticated-unsafe-2xx.

Everything runs against ``examples/reference-mcp/`` on loopback or as a
subprocess. Nothing here reaches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from itest.probes.mcp import (
    CallResult,
    McpProbeError,
    McpTarget,
    ToolInfo,
    call_tool,
    classify_mutation,
    list_tools,
)

# --- load the reference server by path (its directory name is not importable) ---

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "reference-mcp" / "server.py"
)
_spec = importlib.util.spec_from_file_location("reference_mcp_server", _SERVER_PATH)
assert _spec and _spec.loader
reference_mcp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reference_mcp
_spec.loader.exec_module(reference_mcp)

EXPECTED_TOOLS = {
    "get_guide",
    "search_records",
    "fetch_record",
    "create_record",
    "update_record",
    "delete_record",
    "lookalike_read",
    "enrich",
}

#: An id no record can have. Every mutating call below carries one of these:
#: the probe proves a guard by being ADMITTED, never by destroying something.
SENTINEL_ID = "sentinel-cannot-exist-0000"

#: A distinctive token, so "did the credential leak?" is a substring search with
#: no chance of a coincidental match.
DISTINCTIVE_TOKEN = "zzq-distinctive-credential-4b17f9-do-not-log"

CREDENTIAL_ENV = "ITEST_MCP_PROBE_TOKEN"


@pytest.fixture(scope="session")
def reference() -> Iterator[Any]:
    """The reference MCP server on a loopback port, guarded and open mounts."""
    with reference_mcp.serve_in_thread(token=DISTINCTIVE_TOKEN) as running:
        yield running


@pytest.fixture
def credential_dir(tmp_path: Path) -> Path:
    """A checkout-shaped directory holding a gitignored-style ``.itest/.env``.

    The token reaches the probe the way the HTTP probe's does: written to a
    file, named by env var, resolved at call time. It is never a literal in a
    probe call.
    """
    itest = tmp_path / ".itest"
    itest.mkdir()
    (itest / ".env").write_text(
        f"{CREDENTIAL_ENV}={DISTINCTIVE_TOKEN}\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _no_ambient_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell wins over ``.itest/.env`` by design, so make sure the shell is
    silent — otherwise a stray export could make a credential test pass for the
    wrong reason."""
    monkeypatch.delenv(CREDENTIAL_ENV, raising=False)


def _stdio_target(**kwargs: Any) -> McpTarget:
    return McpTarget(
        kind="stdio", command=[sys.executable, str(_SERVER_PATH)], **kwargs
    )


def _by_name(tools: list[ToolInfo]) -> dict[str, ToolInfo]:
    return {tool.name: tool for tool in tools}


# --- list_tools ---------------------------------------------------------------


def test_list_tools_over_stdio_returns_the_eight_tools() -> None:
    tools = list_tools(_stdio_target())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_list_tools_over_http_returns_the_eight_tools(
    reference: Any, credential_dir: Path
) -> None:
    target = McpTarget(
        kind="http", url=reference.guarded_url, credential_env=CREDENTIAL_ENV
    )
    tools = list_tools(target, base_dir=credential_dir)
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_list_tools_carries_annotations_and_schemas() -> None:
    tools = _by_name(list_tools(_stdio_target()))

    assert tools["get_guide"].annotations == {}  # none declared -> {}, never None
    assert tools["fetch_record"].annotations == {"readOnlyHint": True}
    assert tools["delete_record"].annotations["destructiveHint"] is True
    assert tools["create_record"].annotations["readOnlyHint"] is False
    assert tools["enrich"].annotations["openWorldHint"] is True

    assert tools["fetch_record"].input_schema["properties"].keys() == {"id"}
    assert tools["get_guide"].input_schema.get("properties", {}) == {}
    assert tools["delete_record"].input_schema["properties"].keys() == {"id", "confirm"}


def test_hashes_are_stable_across_two_calls() -> None:
    """Both hashes are drift detectors, so they must be a function of the tool
    and nothing else — not of dict ordering, not of when the call was made."""
    first = _by_name(list_tools(_stdio_target()))
    second = _by_name(list_tools(_stdio_target()))

    for name in EXPECTED_TOOLS:
        assert first[name].schema_hash == second[name].schema_hash, name
        assert first[name].description_hash == second[name].description_hash, name
        assert len(first[name].schema_hash) == 12
        assert len(first[name].description_hash) == 12


# A single-tool stdio server whose description and schema are chosen by argv, so
# "what changes which hash?" can be asked by changing exactly one of them.
_VARIANT_SERVER = textwrap.dedent(
    """
    import sys
    from mcp.server.mcpserver import MCPServer

    description, extra = sys.argv[1], sys.argv[2]
    server = MCPServer(name="variant", version="1.0.0")

    if extra == "none":

        @server.tool(name="thing", description=description)
        def thing(a: str) -> str:
            return a

    else:

        @server.tool(name="thing", description=description)
        def thing(a: str, b: str) -> str:
            return a + b

    server.run("stdio")
    """
)


@pytest.fixture
def variant_script(tmp_path: Path) -> Path:
    script = tmp_path / "variant_server.py"
    script.write_text(_VARIANT_SERVER, encoding="utf-8")
    return script


def _variant(script: Path, description: str, extra: str) -> ToolInfo:
    target = McpTarget(
        kind="stdio", command=[sys.executable, str(script), description, extra]
    )
    return _by_name(list_tools(target))["thing"]


def test_a_changed_description_changes_only_the_description_hash(
    variant_script: Path,
) -> None:
    before = _variant(variant_script, "the original wording", "none")
    after = _variant(variant_script, "completely different wording", "none")

    assert after.description_hash != before.description_hash
    assert after.schema_hash == before.schema_hash


def test_a_changed_schema_changes_only_the_schema_hash(variant_script: Path) -> None:
    before = _variant(variant_script, "same wording", "none")
    after = _variant(variant_script, "same wording", "extra")

    assert after.schema_hash != before.schema_hash
    assert after.description_hash == before.description_hash


def test_list_tools_on_the_guarded_mount_without_a_credential_fails(
    reference: Any,
) -> None:
    """No credential named at all -> the guarded mount refuses, and the probe
    says so rather than returning an empty tool list, which would read as a
    server with no tools."""
    target = McpTarget(kind="http", url=reference.guarded_url)
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(target)
    assert "refused" in str(excinfo.value).lower()


def test_list_tools_on_the_open_mount_needs_no_credential(reference: Any) -> None:
    """THE INTENDED RED. Anyone can enumerate the open mount's tools."""
    target = McpTarget(kind="http", url=reference.open_url)
    assert {tool.name for tool in list_tools(target)} == EXPECTED_TOOLS


# --- classify_mutation --------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "expected_class", "expected_source"),
    [
        # The annotation and the name agree; the annotation is what decided.
        ("delete_record", "destructive", "annotation"),
        ("create_record", "write", "annotation"),
        ("update_record", "write", "annotation"),
        ("fetch_record", "read", "annotation"),
        ("search_records", "read", "annotation"),
        # No annotations at all and no parameters: only the name can classify it.
        ("get_guide", "informational", "name"),
        # NOT a conflict, and deliberately so. `lookalike_read`'s annotation says
        # read-only and its name says nothing that disagrees, so the classifier
        # has nothing to flag. Its conflict is BEHAVIORAL — it mutates when
        # called — which no reading of tools/list can see. Catching that is
        # P31's B1 recipe. Do not "fix" this expectation to a conflict.
        ("lookalike_read", "read", "annotation"),
        # Named nothing in particular; the annotation carries it.
        ("enrich", "read", "annotation"),
    ],
)
def test_classify_mutation_on_the_reference_tools(
    tool_name: str, expected_class: str, expected_source: str
) -> None:
    tools = _by_name(list_tools(_stdio_target()))
    assert classify_mutation(tools[tool_name]) == (expected_class, expected_source)


def _synthetic(name: str, annotations: dict[str, Any], params: list[str]) -> ToolInfo:
    """A ToolInfo built by hand, for the classifier branches the reference
    server deliberately does not contain."""
    return ToolInfo(
        name=name,
        description="synthetic",
        input_schema={"type": "object", "properties": dict.fromkeys(params, {})},
        output_schema=None,
        annotations=annotations,
        schema_hash="0" * 12,
        description_hash="0" * 12,
    )


def test_annotation_beats_a_disagreeing_name_and_reports_the_conflict() -> None:
    """A tool named `delete_*` that declares itself read-only. The annotation
    wins — it is the server's own statement — but the disagreement is REPORTED
    in the source rather than resolved away, because one of the two is wrong and
    the probe cannot know which."""
    info = _synthetic("delete_thing", {"readOnlyHint": True}, ["id"])
    assert classify_mutation(info) == ("read", "conflict:name-says-destructive")


def test_a_read_annotation_on_a_write_name_is_also_a_conflict() -> None:
    info = _synthetic("create_thing", {"readOnlyHint": True}, ["name"])
    assert classify_mutation(info) == ("read", "conflict:name-says-write")


def test_a_destructive_annotation_on_a_read_name_is_a_conflict() -> None:
    info = _synthetic("fetch_thing", {"destructiveHint": True}, ["id"])
    assert classify_mutation(info) == ("destructive", "conflict:name-says-read")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("delete_thing", "destructive"),
        ("remove_thing", "destructive"),
        ("purge_thing", "destructive"),
        ("create_thing", "write"),
        ("update_thing", "write"),
        ("set_thing", "write"),
        ("link_thing", "write"),
        ("unlink_thing", "write"),
        ("fetch_thing", "read"),
        ("search_thing", "read"),
        ("list_thing", "read"),
        ("aggregate_thing", "read"),
    ],
)
def test_the_name_heuristic_when_no_annotations_exist(name: str, expected: str) -> None:
    assert classify_mutation(_synthetic(name, {}, ["id"])) == (expected, "name")


def test_a_parameterless_getter_is_informational_but_a_parameterised_one_reads() -> (
    None
):
    """`informational` is the narrow case: a getter that takes no arguments
    cannot be pointed at anything, so calling it discloses only what the server
    volunteers. Give it a parameter and it is an ordinary read."""
    assert classify_mutation(_synthetic("get_thing", {}, [])) == (
        "informational",
        "name",
    )
    assert classify_mutation(_synthetic("get_thing", {}, ["id"])) == ("read", "name")


def test_unknown_when_neither_annotation_nor_name_decides() -> None:
    assert classify_mutation(_synthetic("frobnicate", {}, ["x"])) == (
        "unknown",
        "unknown",
    )


# --- call_tool: the refuse-by-default gate ------------------------------------

# A command that leaves a marker file behind the instant it is launched. Used as
# a probe target so "no connection was opened" is provable by the file's absence
# rather than by trusting the probe's own report.
_MARKER_SERVER = textwrap.dedent(
    """
    import pathlib, sys
    pathlib.Path(sys.argv[1]).write_text("launched", encoding="utf-8")
    """
)


@pytest.fixture
def marker(tmp_path: Path) -> tuple[McpTarget, Path]:
    script = tmp_path / "marker_server.py"
    script.write_text(_MARKER_SERVER, encoding="utf-8")
    evidence = tmp_path / "launched.txt"
    target = McpTarget(
        kind="stdio", command=[sys.executable, str(script), str(evidence)]
    )
    return target, evidence


@pytest.mark.parametrize("mutation_class", ["write", "destructive"])
def test_a_mutating_call_is_refused_before_anything_is_launched(
    marker: tuple[McpTarget, Path], mutation_class: str
) -> None:
    """The default is refuse, and the refusal happens BEFORE the transport is
    touched. If the probe had connected, the marker file would exist."""
    target, evidence = marker
    result = call_tool(
        target,
        "delete_record",
        {"id": SENTINEL_ID, "confirm": True},
        authenticated=False,
        mutation_class=mutation_class,
    )
    assert result.status == "refused"
    assert result.ok is False
    assert result.raw is None
    assert not evidence.exists(), "the probe launched the target despite refusing"


def test_a_read_is_not_gated(marker: tuple[McpTarget, Path]) -> None:
    """Proof the gate is about the mutation class and not about refusing
    everything: a read reaches the transport (and here fails, because the
    marker command speaks no MCP — which is an `error`, not a `refused`)."""
    target, evidence = marker
    result = call_tool(
        target,
        "fetch_record",
        {"id": "r-1"},
        authenticated=False,
        mutation_class="read",
    )
    assert result.status == "error"
    assert evidence.exists(), "a read should have reached the transport"


def test_an_unknown_mutation_class_is_not_gated(
    marker: tuple[McpTarget, Path],
) -> None:
    """Only write and destructive are gated. `unknown` is not treated as
    mutating: gating it would make the probe refuse most of a real server, and
    the honest place to decide an unknown tool's fate is the caller."""
    target, evidence = marker
    result = call_tool(
        target, "frobnicate", {}, authenticated=False, mutation_class=None
    )
    assert result.status == "error"
    assert evidence.exists()


def test_allow_mutating_opens_the_gate(reference: Any) -> None:
    target = McpTarget(kind="http", url=reference.open_url)
    result = call_tool(
        target,
        "delete_record",
        {"id": SENTINEL_ID, "confirm": True},
        authenticated=False,
        allow_mutating=True,
        mutation_class="destructive",
    )
    assert result.status != "refused"


# --- call_tool: outcomes against the reference server -------------------------


def test_unauthenticated_call_on_the_guarded_mount_is_refused(reference: Any) -> None:
    target = McpTarget(kind="http", url=reference.guarded_url)
    result = call_tool(
        target,
        "fetch_record",
        {"id": "r-1"},
        authenticated=False,
        mutation_class="read",
    )
    assert result.status == "refused"
    assert result.ok is False


def test_unauthenticated_read_on_the_open_mount_is_ok(reference: Any) -> None:
    """An unauthenticated READ succeeding is a finding for a later recipe to
    weigh — it is not CRITICAL here, because nothing was mutated. The probe
    reports what happened; it does not editorialise."""
    target = McpTarget(kind="http", url=reference.open_url)
    result = call_tool(
        target,
        "fetch_record",
        {"id": "r-1"},
        authenticated=False,
        mutation_class="read",
    )
    assert result.status == "ok"
    assert result.ok is True
    assert result.raw is not None


def test_unauthenticated_destructive_call_on_the_open_mount_is_critical(
    reference: Any,
) -> None:
    """THE CRITICAL CATCH. An anonymous caller reached a destructive tool and the
    server answered with a successful result. The id is a sentinel, so nothing
    was destroyed to learn it — the finding is that the call was admitted."""
    target = McpTarget(kind="http", url=reference.open_url)
    result = call_tool(
        target,
        "delete_record",
        {"id": SENTINEL_ID, "confirm": True},
        authenticated=False,
        allow_mutating=True,
        mutation_class="destructive",
    )
    assert result.status == "critical"
    assert result.ok is False
    assert SENTINEL_ID not in reference.records


def test_unauthenticated_write_on_the_open_mount_is_critical(reference: Any) -> None:
    target = McpTarget(kind="http", url=reference.open_url)
    result = call_tool(
        target,
        "create_record",
        {"name": SENTINEL_ID},
        authenticated=False,
        allow_mutating=True,
        mutation_class="write",
    )
    assert result.status == "critical"


def test_an_authenticated_destructive_call_is_not_critical(
    reference: Any, credential_dir: Path
) -> None:
    """CRITICAL is about who got in, not about what the tool does. With a valid
    credential the same call is an ordinary result."""
    target = McpTarget(
        kind="http", url=reference.guarded_url, credential_env=CREDENTIAL_ENV
    )
    result = call_tool(
        target,
        "delete_record",
        {"id": SENTINEL_ID, "confirm": True},
        authenticated=True,
        allow_mutating=True,
        mutation_class="destructive",
        base_dir=credential_dir,
    )
    assert result.status == "ok"


def test_authenticated_read_on_the_guarded_mount_is_ok(
    reference: Any, credential_dir: Path
) -> None:
    target = McpTarget(
        kind="http", url=reference.guarded_url, credential_env=CREDENTIAL_ENV
    )
    result = call_tool(
        target,
        "fetch_record",
        {"id": "r-1"},
        authenticated=True,
        mutation_class="read",
        base_dir=credential_dir,
    )
    assert result.status == "ok"
    assert "widget" in json.dumps(result.raw)


def test_a_tool_error_result_is_an_error_not_ok(reference: Any) -> None:
    """`fetch_record` on an unknown id answers `isError=true`. That is a failed
    call, not a successful one, and the probe must not report it as `ok`."""
    target = McpTarget(kind="http", url=reference.open_url)
    result = call_tool(
        target,
        "fetch_record",
        {"id": "no-such-record"},
        authenticated=False,
        mutation_class="read",
    )
    assert result.status == "error"
    assert "404" in result.detail or "404" in json.dumps(result.raw)


def test_call_over_stdio_works() -> None:
    result = call_tool(
        _stdio_target(), "get_guide", {}, authenticated=False, mutation_class="read"
    )
    assert result.status == "ok"


# --- the credential never leaks ----------------------------------------------

# A stdio server that hands its caller back whatever the credential env var
# holds. A probe that did not scrub would put the token straight into `raw`.
_ECHO_SERVER = textwrap.dedent(
    """
    import os
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="echo", version="1.0.0")

    @server.tool(name="echo_credential", description="Echo the credential back.")
    def echo_credential() -> str:
        return "token-is: " + os.environ.get("ITEST_MCP_PROBE_TOKEN", "(unset)")

    server.run("stdio")
    """
)


@pytest.fixture
def echo_target(tmp_path: Path) -> McpTarget:
    script = tmp_path / "echo_server.py"
    script.write_text(_ECHO_SERVER, encoding="utf-8")
    return McpTarget(
        kind="stdio",
        command=[sys.executable, str(script)],
        credential_env=CREDENTIAL_ENV,
    )


def test_the_credential_reaches_a_stdio_server_by_env_var_name(
    echo_target: McpTarget, credential_dir: Path
) -> None:
    """The stdio analogue of an Authorization header: the value is placed in the
    subprocess's environment under the name the target records. Proven by the
    server reporting the var as set — the probe scrubs the value itself, so what
    comes back says the token arrived without repeating it."""
    result = call_tool(
        echo_target,
        "echo_credential",
        {},
        authenticated=True,
        mutation_class="read",
        base_dir=credential_dir,
    )
    assert result.status == "ok"
    assert "(unset)" not in json.dumps(result.raw)


def test_the_credential_is_withheld_from_an_unauthenticated_call(
    echo_target: McpTarget, credential_dir: Path
) -> None:
    """`authenticated=False` means the subprocess never sees the token, even
    though the target names where to find it."""
    result = call_tool(
        echo_target,
        "echo_credential",
        {},
        authenticated=False,
        mutation_class="read",
        base_dir=credential_dir,
    )
    assert result.status == "ok"
    assert "(unset)" in json.dumps(result.raw)


def test_the_credential_never_appears_in_the_result(
    echo_target: McpTarget, credential_dir: Path
) -> None:
    """The server echoes the token back verbatim and the probe still does not
    hand it on — not in `detail`, not in `raw`, not in the repr. A probe result
    is printed, logged and pasted into issues; it must be safe to."""
    result = call_tool(
        echo_target,
        "echo_credential",
        {},
        authenticated=True,
        mutation_class="read",
        base_dir=credential_dir,
    )
    assert DISTINCTIVE_TOKEN not in result.detail
    assert DISTINCTIVE_TOKEN not in json.dumps(result.raw)
    assert DISTINCTIVE_TOKEN not in repr(result)
    assert DISTINCTIVE_TOKEN not in str(result)


def test_the_credential_never_appears_in_a_failed_call(
    credential_dir: Path, tmp_path: Path
) -> None:
    """The failure path is where secrets usually escape — into an exception
    message that names the request. Point an authenticated call at a command
    that is not an MCP server at all and check the wreckage."""
    script = tmp_path / "not_a_server.py"
    script.write_text("raise SystemExit('boom')\n", encoding="utf-8")
    target = McpTarget(
        kind="stdio",
        command=[sys.executable, str(script)],
        credential_env=CREDENTIAL_ENV,
        timeout_s=5.0,
    )
    result = call_tool(
        target,
        "anything",
        {},
        authenticated=True,
        mutation_class="read",
        base_dir=credential_dir,
    )
    assert result.status == "error"
    assert DISTINCTIVE_TOKEN not in result.detail
    assert DISTINCTIVE_TOKEN not in repr(result)


def test_the_credential_never_appears_in_a_raised_exception(
    reference: Any, credential_dir: Path
) -> None:
    """`list_tools` raises rather than returning a status, so its exception is
    the leak surface. Point it at a URL with nothing behind it while a
    credential is configured."""
    target = McpTarget(
        kind="http",
        url="http://127.0.0.1:9/mcp",
        credential_env=CREDENTIAL_ENV,
        timeout_s=5.0,
    )
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(target, base_dir=credential_dir)
    assert DISTINCTIVE_TOKEN not in str(excinfo.value)
    assert DISTINCTIVE_TOKEN not in repr(excinfo.value)


def test_an_authenticated_call_without_a_resolvable_credential_is_a_usage_error(
    tmp_path: Path,
) -> None:
    """Silently probing anonymously when the caller asked for an authenticated
    call would turn a green result into a lie about what was verified."""
    target = McpTarget(
        kind="stdio",
        command=[sys.executable, str(_SERVER_PATH)],
        credential_env=CREDENTIAL_ENV,
    )
    with pytest.raises(McpProbeError) as excinfo:
        call_tool(
            target,
            "get_guide",
            {},
            authenticated=True,
            mutation_class="read",
            base_dir=tmp_path,
        )
    assert CREDENTIAL_ENV in str(excinfo.value)  # the NAME is safe to print


def test_an_authenticated_call_on_a_target_naming_no_credential_is_a_usage_error() -> (
    None
):
    with pytest.raises(McpProbeError):
        call_tool(
            _stdio_target(),
            "get_guide",
            {},
            authenticated=True,
            mutation_class="read",
        )


# --- timeouts are a distinct outcome from refusals ---------------------------

_SLOW_SERVER = textwrap.dedent(
    """
    import time
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="slow", version="1.0.0")

    @server.tool(name="sleep", description="Sleep for a while.")
    def sleep(seconds: float) -> str:
        time.sleep(seconds)
        return "done"

    server.run("stdio")
    """
)


@pytest.fixture
def slow_target(tmp_path: Path) -> McpTarget:
    script = tmp_path / "slow_server.py"
    script.write_text(_SLOW_SERVER, encoding="utf-8")
    return McpTarget(kind="stdio", command=[sys.executable, str(script)], timeout_s=2.0)


def test_a_hang_is_an_error_and_says_so(slow_target: McpTarget) -> None:
    """A server that never answers must not look like one that refused. The
    probe returns after its own timeout, and the detail names the limit."""
    result = call_tool(
        slow_target,
        "sleep",
        {"seconds": 30},
        authenticated=False,
        mutation_class="read",
    )
    assert result.status == "error"
    assert result.ok is False
    assert "timed out" in result.detail.lower()
    assert "2.0" in result.detail


def test_a_call_inside_the_timeout_still_succeeds(slow_target: McpTarget) -> None:
    """Proof the timeout is a bound and not a blanket failure."""
    result = call_tool(
        slow_target, "sleep", {"seconds": 0}, authenticated=False, mutation_class="read"
    )
    assert result.status == "ok"


# --- target validation --------------------------------------------------------


def test_an_http_target_without_a_url_is_a_usage_error() -> None:
    with pytest.raises(McpProbeError):
        list_tools(McpTarget(kind="http"))


def test_a_stdio_target_without_a_command_is_a_usage_error() -> None:
    with pytest.raises(McpProbeError):
        list_tools(McpTarget(kind="stdio"))


def test_an_unknown_kind_is_a_usage_error() -> None:
    with pytest.raises(McpProbeError):
        list_tools(McpTarget(kind="carrier-pigeon", url="http://example.test"))  # type: ignore[arg-type]


def test_call_result_and_tool_info_are_frozen() -> None:
    """Both are values a caller may hold on to across a run; neither may be
    edited after the fact into saying something the probe did not observe."""
    result = CallResult(ok=True, status="ok", detail="x", raw=None)
    with pytest.raises(Exception):  # noqa: B017 - dataclasses raise FrozenInstanceError
        result.status = "refused"  # type: ignore[misc]
    info = _synthetic("thing", {}, [])
    with pytest.raises(Exception):  # noqa: B017
        info.name = "other"  # type: ignore[misc]
