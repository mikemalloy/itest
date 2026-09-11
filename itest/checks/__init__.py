"""The check library: run one trait's check for one declared MCP tool.

Most traits are **engine checks**. ITest runs them from the manifest's
``mcp_tool`` point and the server's live tool list; they produce no per-tool
file, so there is nothing to go stale. A few traits need facts only a person can
supply (a second tenant's credential, where an audit record lands); those are
**generated checks** — a one-line binding in the project's test suite that calls
:func:`run_generated_check` with the fixtures a human owns.

The whole public surface::

    CheckResult(status, detail, evidence=None)
    run_engine_check(trait_id, point, target, *, authenticated) -> CheckResult
    run_generated_check(trait_id, point, target, *, fixtures) -> CheckResult
    clear_cache()   # once per verify run: forget cached listings

``point`` is the manifest's ``mcp_tool`` point as a plain dict (``id``, ``type``,
``server``, ``target``, ``attributes``). Neither entry point raises for an
unknown trait or an unusable server: each returns ``not_verifiable`` with the
reason, because a check that could not run must never read as one that passed.

Engine checks today: A1 (:mod:`.authority`), B1 agreement (:mod:`.blast_radius`),
D1–D3 (:mod:`.change`). The generated registry is empty until the A2/B2/B4
recipes ship. ``docs/checks.md`` is the reference.
"""

from __future__ import annotations

from itest.checks import authority, blast_radius, change
from itest.checks._base import (
    ENGINE_CHECKS,
    GENERATED_CHECKS,
    STATUSES,
    CheckResult,
    clear_cache,
    not_verifiable,
    scrub_result,
)
from itest.probes.mcp import McpTarget

__all__ = [
    "ENGINE_CHECKS",
    "GENERATED_CHECKS",
    "STATUSES",
    "CheckResult",
    "authority",
    "blast_radius",
    "change",
    "clear_cache",
    "run_engine_check",
    "run_generated_check",
]


def run_engine_check(
    trait_id: str, point: dict, target: McpTarget, *, authenticated: bool
) -> CheckResult:
    """Run the engine check for ``trait_id`` on one tool point.

    An unknown trait id is ``not_verifiable`` ("no engine check for <id>"),
    never an exception. The result is scrubbed here as well as in each check,
    so a check registered by any route cannot return an unscrubbed string.
    """
    check = ENGINE_CHECKS.get(trait_id)
    if check is None:
        return not_verifiable(f"no engine check for {trait_id}")
    return scrub_result(check(point, target, authenticated=authenticated), target)


def run_generated_check(
    trait_id: str, point: dict, target: McpTarget, *, fixtures: dict
) -> CheckResult:
    """Run the generated check for ``trait_id`` with the human-owned fixtures.

    The registry is empty today, so every id is ``not_verifiable`` ("no
    generated check for <id> yet"). The signature exists now so the thin
    bindings sync writes import cleanly before the checks behind them ship.
    """
    check = GENERATED_CHECKS.get(trait_id)
    if check is None:
        return not_verifiable(f"no generated check for {trait_id} yet")
    return scrub_result(check(point, target, fixtures=fixtures), target)
