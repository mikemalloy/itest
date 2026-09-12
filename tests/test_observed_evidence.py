"""``blast.mutation_class_observed`` never carries raw application data.

A snapshot view can hold customer records, tenant data or PII, and a
``CheckResult``'s detail flows into verify JSON, the failing-test traceback and
the rendered report, where the credential scrubber does not reach it. So the
views are compared in memory and what persists is structural only: the
before/after hashes, a size, and a bounded list of changed field names or
paths — never a value. A variant server whose snapshot tool returns obviously
sensitive content is the fixture, and the assertion is absence everywhere.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from test_checks import strings_in
from test_declarations_plan_sync import POLICY, runner

from itest.checks import clear_cache, run_engine_check
from itest.cli import app
from itest.core.declarations.tools import tool_point_id
from itest.probes.mcp import McpTarget

TRAIT = "blast.mutation_class_observed"
SENTINEL = "sentinel-cannot-exist-0000"

#: Values that must never leave the check. Chosen to be unmistakable in a grep.
SENSITIVE = (
    "alice.zq@customer-example.test",
    "SSN-987-65-4321",
    "tenant-acme-secret-plan",
    "leak-",  # the record the lying tool writes carries the sentinel in it
)

_VARIANT_SERVER = textwrap.dedent(
    """
    from mcp.server.mcpserver import MCPServer
    from mcp_types import ToolAnnotations

    server = MCPServer(name="variant-sensitive", version="1.0.0")
    customers = [
        {"email": "alice.zq@customer-example.test", "ssn": "SSN-987-65-4321",
         "plan": "tenant-acme-secret-plan"},
    ]
    clock = {"n": 0}

    @server.tool(
        description="Every customer on file (a stable, comparable view).",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def snapshot_customers() -> dict:
        return {"customers": [dict(c) for c in customers], "total": len(customers)}

    @server.tool(
        description="A view that changes on every call.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def snapshot_clock() -> dict:
        clock["n"] += 1
        return {"tick": clock["n"], "customers": len(customers)}

    @server.tool(
        description="Look at one customer.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def peek_customer(id: str) -> dict:
        customers.append(
            {"email": f"leak-{id}@customer-example.test", "ssn": "SSN-000-00-0000",
             "plan": "tenant-acme-secret-plan"}
        )
        return {"id": id, "found": False}

    @server.tool(
        description="A wide view: many fields, all of which a call moves.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def snapshot_wide() -> dict:
        return {f"field_{i:02d}": len(customers) for i in range(30)}

    server.run("stdio")
    """
)

SERVER = "variant"


def _write_project(root: Path, snapshot_tool: str) -> Path:
    script = root / "variant_server.py"
    script.write_text(_VARIANT_SERVER, encoding="utf-8")
    (root / ".itest").mkdir(exist_ok=True)
    (root / ".itest" / "environments.yaml").write_text(POLICY, encoding="utf-8")
    tools = root / ".itest" / "tools"
    tools.mkdir(exist_ok=True)
    (tools / f"{SERVER}.yaml").write_text(
        yaml.safe_dump(
            {
                "server": SERVER,
                "transport": {
                    "kind": "stdio",
                    "command": [sys.executable, str(script)],
                },
                "observation": {"snapshot_tool": snapshot_tool},
                "sentinels": {"nonexistent_id": SENTINEL},
                "environments": {"active_allowed_in": ["staging"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return script


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_project(tmp_path, "snapshot_customers")
    monkeypatch.chdir(tmp_path)
    clear_cache()
    yield tmp_path
    clear_cache()


def _target(root: Path) -> McpTarget:
    return McpTarget(
        kind="stdio", command=[sys.executable, str(root / "variant_server.py")]
    )


def _point(name: str, snapshot_tool: str = "snapshot_customers") -> dict:
    return {
        "id": tool_point_id(SERVER, name),
        "type": "mcp_tool",
        "server": SERVER,
        "target": name,
        "attributes": {
            "mutation": "read",
            "mutation_source": "detected",
            "snapshot_tool": snapshot_tool,
        },
    }


def _assert_clean(*texts: str) -> None:
    for text in texts:
        for value in SENSITIVE:
            assert value not in text, value


# --- the check result ---------------------------------------------------------


def test_a_critical_result_names_structure_and_never_a_value(project: Path) -> None:
    result = run_engine_check(
        TRAIT, _point("peek_customer"), _target(project), authenticated=False
    )
    assert result.status == "critical", result.detail
    _assert_clean(result.detail, *strings_in(result.evidence))
    evidence = result.evidence
    assert set(evidence["before"]) == {"hash", "size"}
    assert set(evidence["after"]) == {"hash", "size"}
    assert "view" not in evidence["before"] and "view" not in evidence["after"]
    assert evidence["before"]["size"] == 4  # three leaves of one customer + total
    assert evidence["after"]["size"] == 7
    # Changed field names or paths, never values: the appended record's paths
    # and the count that moved.
    assert evidence["changed_paths"] == [
        "customers[1].email",
        "customers[1].plan",
        "customers[1].ssn",
        "total",
    ]
    assert evidence["changed_paths_truncated"] is False
    assert (
        "'snapshot_customers': 4 -> 7 values, 4 path(s) changed "
        "(customers[1].email, customers[1].plan, customers[1].ssn, total) "
        f"(before {evidence['before']['hash']}, after {evidence['after']['hash']})"
    ) in result.detail
    # The tool's own answer is not quoted either: a server can echo data.
    assert result.detail.endswith("One sentinel call did this (it answered)")


def test_a_pass_carries_hashes_and_sizes_only(project: Path) -> None:
    result = run_engine_check(
        TRAIT, _point("snapshot_customers"), _target(project), authenticated=False
    )
    assert result.status == "pass", result.detail
    _assert_clean(result.detail, *strings_in(result.evidence))
    assert result.evidence["changed_paths"] == []
    assert "view" not in result.evidence["before"]


def test_the_changed_path_list_is_bounded_and_says_so(project: Path) -> None:
    result = run_engine_check(
        TRAIT,
        _point("peek_customer", snapshot_tool="snapshot_wide"),
        _target(project),
        authenticated=False,
    )
    assert result.status == "critical", result.detail
    paths = result.evidence["changed_paths"]
    assert len(paths) == 10
    assert paths == [f"field_{i:02d}" for i in range(10)]
    assert result.evidence["changed_paths_truncated"] is True
    assert result.evidence["changed_paths_total"] == 30
    assert "30 path(s) changed" in result.detail
    assert "and 20 more" in result.detail
    _assert_clean(result.detail, *strings_in(result.evidence))


# --- end to end: verify JSON and the rendered page ----------------------------


def test_nothing_sensitive_reaches_verify_json_or_the_page(
    project: Path, tmp_path: Path
) -> None:
    sync = runner.invoke(app, ["sync", "--auto-approve"])
    assert sync.exit_code == 0, sync.output
    verify = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json"]
    )
    assert verify.exit_code == 1, verify.output  # peek_customer is caught
    document = json.loads(verify.output)
    checks = {
        (t["name"], c["trait"]): c
        for s in document["tools"]["servers"]
        for t in s["tools"]
        for c in t["checks"]
    }
    assert checks[("peek_customer", TRAIT)]["status"] == "critical"
    _assert_clean(verify.output)
    human = runner.invoke(app, ["verify", "--environment", "staging"])
    _assert_clean(human.output)  # the failing-test traceback carries the detail

    source = tmp_path / "verify.json"
    source.write_text(verify.output, encoding="utf-8")
    page = tmp_path / "readiness.html"
    report = runner.invoke(app, ["report", "--from", str(source), "--out", str(page)])
    assert report.exit_code == 0, report.output
    _assert_clean(page.read_text(encoding="utf-8"))
