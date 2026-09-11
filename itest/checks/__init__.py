"""CONTRACT STUB — the engine-check library is 31A's, and replaces this module.

This file exists only so the trait-table work can code against the agreed
contract before 31A lands. It holds no logic and must not grow any:

    from itest.checks import run_engine_check
    run_engine_check(trait_id: str, point: dict, target: McpTarget,
                     *, authenticated: bool) -> CheckResult
    CheckResult: dataclass(status: str, detail: str, evidence: dict | None)
    status ∈ {"pass", "fail", "critical", "changed", "not_verifiable"}
    run_generated_check(trait_id, point, target, *, fixtures: dict) -> CheckResult

Until 31A lands, both functions raise ``NotImplementedError("31A")``. The
engine runtime (``itest.traits.runtime``) turns that into a skip, so an engine
check that does not exist yet reads as "not run", never as an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # a type only: importing this module loads no transport
    from itest.probes.mcp import McpTarget


@dataclass
class CheckResult:
    status: str
    detail: str
    evidence: dict | None


def run_engine_check(
    trait_id: str, point: dict, target: McpTarget, *, authenticated: bool
) -> CheckResult:
    raise NotImplementedError("31A")


def run_generated_check(
    trait_id: str, point: dict, target: McpTarget, *, fixtures: dict
) -> CheckResult:
    raise NotImplementedError("31A")
