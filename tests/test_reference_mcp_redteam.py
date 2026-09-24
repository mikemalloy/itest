"""The red-team harness under ``examples/reference-mcp/redteam/`` is portable.

The harness — a promptfoo config, an MCP-connected agent, a stdio proxy that
can poison one tool's result, and two launch scripts — ran on a Mac on
2026-09-23 and runs unchanged on ubuntu-latest from the scheduled job. That
portability is the property under test here: no absolute path anywhere, no
credential-shaped string, a proxy that needs nothing but the standard library,
launch scripts that resolve everything from their own directory, and a config
whose providers name those scripts and nothing else. The evidence source that
joins the harness's results onto the manifest is loaded through the real
loader with its variable unset, and must be a reason, not an exception.

The smoke tests drive the two launch scripts over stdio for real — no model,
no network, no key — from a directory that is not the harness's own.
"""

from __future__ import annotations

import ast
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from itest.core.evidence import load_sources
from itest.core.evidence.join import unreadable_source
from itest.core.redact import _CREDENTIAL_PATTERNS
from itest.core.syncer import render_evidence_line

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "reference-mcp"
REDTEAM_DIR = EXAMPLE_DIR / "redteam"
SOURCE_FILE = EXAMPLE_DIR / ".itest" / "sources" / "promptfoo-ci.yaml"

CONFIG = REDTEAM_DIR / "promptfooconfig.yaml"
AGENT = REDTEAM_DIR / "agent.js"
PROXY = REDTEAM_DIR / "poison-proxy.py"
PROMPT = REDTEAM_DIR / "prompt.json"
CLEAN = REDTEAM_DIR / "proxy-clean.sh"
POISONED = REDTEAM_DIR / "proxy-poisoned.sh"

#: The variable the scheduled job sets to wherever it wrote promptfoo's output.
RESULTS_ENV = "ITEST_PROMPTFOO_RESULTS"

#: The reference server's eight tools (pinned in tests/test_reference_mcp.py).
TOOLS = {
    "get_guide",
    "search_records",
    "fetch_record",
    "create_record",
    "update_record",
    "delete_record",
    "lookalike_read",
    "enrich",
}

#: The injected instruction's sentinel: what the poisoned guide must carry and
#: the clean one must not.
PAYLOAD_MARKER = "ZZZ-ITEST-SENTINEL"

#: The exact invocation the README documents and the scheduled job runs.
NPX_COMMAND = "npx -y promptfoo@0.123.1"

_ABSOLUTE_PATH = re.compile(r"/(?:Volumes|sessions|Users|home)/")
_LEADING_SLASH = re.compile(r"^\s*/(?![*/])")


def _harness_files() -> list[Path]:
    return sorted(
        p
        for p in REDTEAM_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


def _harness_ids() -> list[str]:
    return [p.name for p in _harness_files()]


# --- portability: no absolute paths, no credentials ---------------------------


def test_the_harness_ships_exactly_these_files() -> None:
    assert _harness_ids() == [
        "README.md",
        "agent.js",
        "corpus.md",
        "poison-proxy.py",
        "prompt.json",
        "promptfooconfig.yaml",
        "proxy-clean.sh",
        "proxy-poisoned.sh",
    ]


def test_the_mac_only_config_is_gone() -> None:
    """One config for every machine; a `-mac` variant would be the drift."""
    assert not (REDTEAM_DIR / "promptfooconfig-mac.yaml").exists()
    assert not list(REDTEAM_DIR.glob("*-mac.*"))


@pytest.mark.parametrize("path", _harness_files(), ids=_harness_ids())
def test_no_absolute_path_in_the_harness(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not _ABSOLUTE_PATH.search(text), f"{path.name} names a machine's path"
    for number, line in enumerate(text.splitlines(), start=1):
        if number == 1 and line.startswith("#!"):
            continue
        # A line that opens with a slash is a path — unless it opens a JS
        # comment (`/**`, `//`), which is the only other thing that does.
        assert not _LEADING_SLASH.match(line), (
            f"{path.name}:{number} starts with a slash: {line.strip()!r}"
        )


@pytest.mark.parametrize("path", _harness_files(), ids=_harness_ids())
def test_no_credential_shaped_string_in_the_harness(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for name, pattern in _CREDENTIAL_PATTERNS:
        assert not pattern.search(text), f"{path.name} holds a {name}"
    assert "sk-ant-" not in text


# --- the proxy: stdlib only -----------------------------------------------------


def test_poison_proxy_imports_only_the_standard_library() -> None:
    tree = ast.parse(PROXY.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.partition(".")[0])
    assert imported, "the proxy imports nothing?"
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names


# --- the launch scripts ---------------------------------------------------------


@pytest.mark.parametrize("script", [CLEAN, POISONED], ids=["clean", "poisoned"])
def test_launch_scripts_are_executable_sh_resolving_from_their_own_directory(
    script: Path,
) -> None:
    assert script.is_file()
    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert 'HERE=$(cd "$(dirname "$0")" && pwd)' in text
    # python3 from PATH, the proxy and the server by their own directory.
    assert 'python3 "$HERE/poison-proxy.py" python3 "$HERE/../server.py"' in text


def test_clean_and_poisoned_differ_only_in_the_poison_flag() -> None:
    clean = CLEAN.read_text(encoding="utf-8")
    poisoned = POISONED.read_text(encoding="utf-8")
    assert "POISON=0 exec python3" in clean
    assert "POISON=1 exec python3" in poisoned
    assert clean.replace("POISON=0 exec", "POISON=1 exec") == poisoned


# --- the config -------------------------------------------------------------------


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_config_providers_are_the_four_agents_over_the_two_scripts() -> None:
    providers = _config()["providers"]
    assert [p["config"]["label"] for p in providers] == [
        "sonnet-clean",
        "sonnet-poisoned",
        "haiku-clean",
        "haiku-poisoned",
    ]
    for provider in providers:
        assert provider["id"] == "file://agent.js"
        config = provider["config"]
        assert config["command"] == "sh"
        assert config["args"] in (["./proxy-clean.sh"], ["./proxy-poisoned.sh"])
        assert config["args"][0].endswith(
            "poisoned.sh" if "poisoned" in config["label"] else "clean.sh"
        )
    # The model ids come from the environment: the workflow pins them, and a
    # run on a laptop sets the same two names.
    models = [p["config"]["model"] for p in providers]
    assert models == [
        "{{ env.AGENT_MODEL }}",
        "{{ env.AGENT_MODEL }}",
        "{{ env.AGENT_MODEL_2 }}",
        "{{ env.AGENT_MODEL_2 }}",
    ]


def test_config_has_the_three_cases_each_guarded_against_delete_record() -> None:
    config = _config()
    tests = config["tests"]
    assert len(tests) == 3
    descriptions = [t["description"] for t in tests]
    assert descriptions[0].startswith("BASELINE")
    assert descriptions[1].startswith("INDIRECT INJECTION")
    assert descriptions[2].startswith("DIRECT INJECTION")
    # The guard is the boundary question asked of every case: did a
    # destructive tool fire? It lives in defaultTest, so every test carries it.
    default_asserts = config["defaultTest"]["assert"]
    for test in tests:
        effective = default_asserts + test.get("assert", [])
        guards = [
            a
            for a in effective
            if a["type"] == "javascript" and "delete_record" in a["value"]
        ]
        assert len(guards) == 1, test["description"]
        assert "throw new Error" in guards[0]["value"]


def test_config_uses_the_shared_prompt_file() -> None:
    assert _config()["prompts"] == ["file://prompt.json"]


def test_prompt_is_a_chat_array_with_the_agent_system_prompt() -> None:
    """The same three cases must be runnable against promptfoo's stock
    providers later without a config rewrite, so the prompt is a chat array
    (system + the user turn), not a bare string."""
    prompt = json.loads(PROMPT.read_text(encoding="utf-8"))
    assert [m["role"] for m in prompt] == ["system", "user"]
    assert prompt[1]["content"] == "{{message}}"
    assert prompt[0]["content"] == _agent_system_prompt()


# --- the agent --------------------------------------------------------------------


def _agent_system_prompt() -> str:
    """The SYSTEM_PROMPT constant, read from agent.js without a JS runtime."""
    text = AGENT.read_text(encoding="utf-8")
    match = re.search(r"^const SYSTEM_PROMPT =\s*(.+?);\s*$", text, re.S | re.M)
    assert match, "agent.js has no top-level SYSTEM_PROMPT constant"
    parts = re.findall(r"'((?:[^'\\]|\\.)*)'", match.group(1))
    assert parts, match.group(1)
    return "".join(parts)


def test_agent_system_prompt_is_a_top_level_constant() -> None:
    prompt = _agent_system_prompt()
    assert prompt.startswith("You are a records assistant")
    assert "Consult the server guide" in prompt
    text = AGENT.read_text(encoding="utf-8")
    assert "this.config.system || parsed.system || SYSTEM_PROMPT" in text


def test_agent_id_is_the_configured_label() -> None:
    """Rows in the results file are keyed by provider id; the label is what
    tells sonnet-clean from haiku-poisoned."""
    text = AGENT.read_text(encoding="utf-8")
    assert re.search(r"id\(\)\s*\{\s*return this\.config\.label", text)


def test_agent_reads_the_key_from_the_environment_only() -> None:
    text = AGENT.read_text(encoding="utf-8")
    assert "process.env.ANTHROPIC_API_KEY" in text
    assert text.count("ANTHROPIC_API_KEY") == 2  # the read and its error line


#: What a node run of the agent reports back, with the Anthropic API stubbed.
_AGENT_DRIVER = """
const Provider = require(process.argv[2]);
const script = process.argv[3];
const prompt = process.argv[4];
const config = JSON.parse(process.argv[5]);
const captured = [];
global.fetch = async (url, init) => {
  captured.push({ url, headers: init.headers, body: JSON.parse(init.body) });
  const reply = { content: [{ type: 'text', text: 'stubbed' }] };
  return { ok: true, json: async () => reply };
};
const options = { config: { command: 'sh', args: [script], ...config } };
const provider = new Provider(options);
provider.callApi(prompt, {}).then((result) => {
  process.stdout.write(JSON.stringify({ id: provider.id(), result, captured }));
});
"""


def _drive_agent(
    tmp_path: Path, prompt: str, config: dict, env: dict[str, str]
) -> dict:
    driver = tmp_path / "drive.js"
    driver.write_text(_AGENT_DRIVER, encoding="utf-8")
    run = subprocess.run(
        [
            "node",
            str(driver),
            str(AGENT),
            str(CLEAN),
            prompt,
            json.dumps(config),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            **env,
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
@pytest.mark.slow
def test_agent_sends_the_chat_array_prompt_with_the_servers_tools(
    tmp_path: Path,
) -> None:
    """The rendered prompt.json arrives as JSON text: its system message is
    the system prompt, its user turn the conversation, and the eight tools
    ride along. The key goes in one header and nowhere else."""
    prompt = json.loads(PROMPT.read_text(encoding="utf-8"))
    prompt[1]["content"] = "hello there"
    driven = _drive_agent(
        tmp_path,
        json.dumps(prompt),
        {"label": "sonnet-clean", "model": "model-under-test"},
        {"ANTHROPIC_API_KEY": "not-a-real-key", "AGENT_MODEL": "unused"},
    )
    assert driven["id"] == "sonnet-clean"
    assert driven["result"] == {
        "output": "stubbed",
        "metadata": {"toolCalls": [], "toolNames": []},
    }
    (call,) = driven["captured"]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "not-a-real-key"
    body = call["body"]
    assert body["model"] == "model-under-test"
    assert body["system"] == _agent_system_prompt()
    assert body["messages"] == [{"role": "user", "content": "hello there"}]
    assert {t["name"] for t in body["tools"]} == TOOLS
    assert "not-a-real-key" not in json.dumps(body)


@needs_node
@pytest.mark.slow
def test_agent_takes_a_bare_prompt_and_the_model_from_the_environment(
    tmp_path: Path,
) -> None:
    driven = _drive_agent(
        tmp_path,
        "plain string prompt",
        {"label": "haiku-clean"},
        {"ANTHROPIC_API_KEY": "not-a-real-key", "AGENT_MODEL": "env-model"},
    )
    (call,) = driven["captured"]
    assert call["body"]["model"] == "env-model"
    assert call["body"]["system"] == _agent_system_prompt()
    assert call["body"]["messages"] == [
        {"role": "user", "content": "plain string prompt"}
    ]


@needs_node
def test_agent_without_a_key_makes_no_request(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    driver = tmp_path / "drive.js"
    driver.write_text(_AGENT_DRIVER, encoding="utf-8")
    run = subprocess.run(
        ["node", str(driver), str(AGENT), str(CLEAN), "x", "{}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    driven = json.loads(run.stdout)
    assert driven["result"] == {"error": "ANTHROPIC_API_KEY is not set"}
    assert driven["captured"] == []


# --- the prose --------------------------------------------------------------------


def test_readme_documents_the_pinned_command_the_cost_and_the_rule() -> None:
    text = (REDTEAM_DIR / "README.md").read_text(encoding="utf-8")
    assert NPX_COMMAND in text
    assert "--no-cache" in text
    assert "never counts" in text
    assert "schedule" in text.lower()
    assert "ANTHROPIC_API_KEY" in text


def test_corpus_names_the_three_cases_and_the_honest_result() -> None:
    text = (REDTEAM_DIR / "corpus.md").read_text(encoding="utf-8")
    for case in ("baseline", "indirect", "direct"):
        assert case in text.lower(), case
    assert "2026-09-23" in text
    assert "delete_record" in text


# --- the declared source ----------------------------------------------------------


def test_the_source_is_tracked_by_git() -> None:
    """`.itest/` is gitignored everywhere, so the file must be force-added.
    Present on disk but untracked passes here and fails for everyone who clones."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run(
        ["git", "ls-files", "--", str(SOURCE_FILE.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked == ["examples/reference-mcp/.itest/sources/promptfoo-ci.yaml"]


def test_the_source_states_the_facts_the_job_relies_on() -> None:
    document = yaml.safe_load(SOURCE_FILE.read_text(encoding="utf-8"))
    assert document == {
        "kind": "promptfoo",
        "server": "reference-mcp",
        "results_env": RESULTS_ENV,
        "agent": "claude-sonnet-5 and claude-haiku-4-5 via redteam/agent.js",
        "standards": ["ASI01", "LLM01"],
        "max_age_days": 7,
    }


def test_the_source_with_its_variable_unset_is_unreadable_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout with no results file — every PR, every laptop that has not
    run the harness — loads the source as a reason, never an exception."""
    monkeypatch.delenv(RESULTS_ENV, raising=False)
    loaded = load_sources(EXAMPLE_DIR)
    assert loaded.errors == []
    (source,) = [s for s in loaded.sources if s.name == "promptfoo-ci"]
    assert source.results_path is None
    assert source.problem == f"results_env {RESULTS_ENV} is not set"
    line = unreadable_source(source, source.problem)
    assert line.status == "unreadable"
    assert RESULTS_ENV in (line.reason or "")
    assert render_evidence_line(line) == (
        f"evidence promptfoo-ci (promptfoo): unreadable: results_env "
        f"{RESULTS_ENV} is not set"
    )


def test_the_source_with_its_variable_set_resolves_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results = tmp_path / "r.json"
    results.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(RESULTS_ENV, str(results))
    (source,) = [
        s for s in load_sources(EXAMPLE_DIR).sources if s.name == "promptfoo-ci"
    ]
    assert source.problem is None
    assert source.results_path == results


# --- smoke: the scripts over stdio, for real --------------------------------------


class _Stdio:
    """A JSON-RPC line client over a launch script's stdin/stdout."""

    def __init__(self, script: Path, cwd: Path) -> None:
        # python3 must be the interpreter this suite runs under: it is the
        # one with the MCP SDK installed. The scripts take it from PATH.
        env = {
            **os.environ,
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        }
        self.proc = subprocess.Popen(
            [str(script)],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _write(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        wanted = self.next_id
        self._write(
            {"jsonrpc": "2.0", "id": wanted, "method": method, "params": params}
        )
        while True:
            line = self.lines.get(timeout=30)
            assert line is not None, f"server closed stdout before answering {method}"
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == wanted:
                return message

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


@pytest.fixture
def stdio(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_Stdio]:
    """The script named by the test's parameter, launched from a directory
    that is not the harness's own: the script must find the server itself."""
    client = _Stdio(request.param, cwd=tmp_path)
    try:
        client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "itest-suite", "version": "0"},
            },
        )
        client.notify("notifications/initialized", {})
        yield client
    finally:
        client.close()


def _tool_names(client: _Stdio) -> set[str]:
    listing = client.request("tools/list", {})
    return {tool["name"] for tool in listing["result"]["tools"]}


def _guide_text(client: _Stdio) -> str:
    result = client.request("tools/call", {"name": "get_guide", "arguments": {}})
    blocks = result["result"]["content"]
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text")


@pytest.mark.slow
@pytest.mark.parametrize("stdio", [CLEAN], indirect=True, ids=["clean"])
def test_proxy_clean_lists_the_eight_tools_and_passes_the_guide_through(
    stdio: _Stdio,
) -> None:
    assert _tool_names(stdio) == TOOLS
    assert PAYLOAD_MARKER not in _guide_text(stdio)


@pytest.mark.slow
@pytest.mark.parametrize("stdio", [POISONED], indirect=True, ids=["poisoned"])
def test_proxy_poisoned_lists_the_eight_tools_and_poisons_the_guide(
    stdio: _Stdio,
) -> None:
    assert _tool_names(stdio) == TOOLS
    text = _guide_text(stdio)
    assert PAYLOAD_MARKER in text
    assert "delete_record" in text
