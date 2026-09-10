"""The ``itest add`` engine: register an existing test onto an existing point.

This is the deliberately narrow version of a command designed on day one. It
takes a test function a human already wrote and records it in the manifest
against a point the manifest already knows — nothing more. It never declares a
new integration point (that is detection's job, and detection reads Terraform,
not a filename), so an unknown point id is refused rather than created.

The entry it writes is **human-owned from birth**: it carries the current file
hash as its ownership hash and a status derived from the body, so from sync's
point of view it is indistinguishable from a stub a human has since taken over.
Sync's existing guarantees then apply unchanged — append-only, never relocate a
test already in the manifest — so a round-trip leaves the entry exactly as add
left it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from itest.core import planner, stubgen
from itest.core.environments import VALID_TIERS
from itest.core.manifest import (
    TestEntry,
    default_resource_group,
    load_manifest,
    save_manifest,
)

# The canonical body classifier lives in syncer; add must not reimplement it,
# so a registered test is classified exactly as sync would classify the same
# body (stub if it still holds the generated skip line, else implemented).
from itest.core.syncer import _status_from_body


class AddError(Exception):
    """A validation problem in ``itest add``. Actionable; maps to exit code 2."""


def _relativize(base_dir: Path, file: Path) -> str:
    """Return ``file`` as a repo-relative POSIX path under ``base_dir``.

    verify addresses tests by a pytest node id rooted at the project, so a test
    that does not live under the project could never be mapped back to a point.
    """
    p = Path(file)
    if p.is_absolute():
        try:
            p = p.relative_to(base_dir)
        except ValueError:
            raise AddError(
                f"Test file must live under the project ({base_dir}): {file}"
            ) from None
    return p.as_posix()


def _function_defined(path: Path, function: str) -> bool:
    """True when ``function`` is defined in ``path`` (parsed, never imported).

    An AST check, not an import: registering a test must not execute the file,
    and a name that merely appears in a comment or a string is not a definition.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
        for node in ast.walk(tree)
    )


def _resolve_tool_point(manifest, server: str, tool: str) -> str:
    """The point id of ``tool`` on ``server``, or an :class:`AddError`.

    Why this exists: a tool point's id is ``hash("mcp_tool", server, name)``, and
    nobody should have to type a hash to register a test they wrote by hand. With
    ``--server`` the ``--point`` value is read as the tool's name instead, and
    resolved against the manifest — not computed and trusted, so a tool the
    project has never planned is refused rather than invented.
    """
    from itest.core.declarations.tools import POINT_TYPE, tool_point_id

    point_id = tool_point_id(server, tool)
    if manifest.get_point(point_id) is not None:
        return point_id
    known = ", ".join(
        sorted(
            p.target
            for p in manifest.points
            if p.type == POINT_TYPE and p.source == server
        )
    )
    raise AddError(
        f"No tool '{tool}' on declared server '{server}' in the manifest. "
        "`itest add --server` registers a test onto a tool the manifest already "
        "knows; run `itest plan && itest sync` to pick up a new one. Known "
        f"tools on '{server}': {known or '(none)'}."
    )


def add_test(
    base_dir: Path,
    point_id: str,
    file: Path,
    function: str,
    tier: str,
    server: str | None = None,
) -> TestEntry:
    """Register ``function`` in ``file`` against ``point_id``. Return the entry.

    With ``server``, ``point_id`` is the *tool name* on that declared server and
    is resolved to the tool point's id. Everything else is unchanged, including
    the AST validation and the human-owned-from-birth entry.

    Every failure is a hard :class:`AddError`: no manifest, an invalid tier, an
    unknown point, a missing file, a function not defined in it, or a duplicate
    ``(path, function)`` registration.
    """
    manifest_file = planner.manifest_path(base_dir)
    if not manifest_file.exists():
        raise AddError("No manifest found. Run `itest plan && itest sync` first.")
    manifest = load_manifest(manifest_file)

    if tier not in VALID_TIERS:
        raise AddError(f"Unknown tier '{tier}'. Valid tiers: {', '.join(VALID_TIERS)}.")

    if server is not None:
        point_id = _resolve_tool_point(manifest, server, point_id)

    if manifest.get_point(point_id) is None:
        # The out-of-scope refusal: add registers onto existing points; it does
        # not declare new ones. Name the ids that do exist so the fix is obvious.
        known = ", ".join(p.id for p in manifest.points) or "(none)"
        raise AddError(
            f"No integration point '{point_id}' in the manifest. `itest add` "
            "registers a test onto an existing point; it does not declare new "
            f"points. Known point ids: {known}."
        )

    rel = _relativize(base_dir, file)
    file_abs = base_dir / rel
    if not file_abs.exists():
        raise AddError(f"Test file not found: {rel}")
    if not _function_defined(file_abs, function):
        raise AddError(f"Function '{function}' is not defined in {rel}.")

    canonical = f"{rel}::{function}"
    for existing in manifest.tests:
        if existing.path == rel and existing.test_name == function:
            raise AddError(
                f"{canonical} is already registered (point {existing.point_id})."
            )

    entry = TestEntry(
        id="t-" + stubgen.content_hash(canonical)[:12],
        point_id=point_id,
        path=rel,
        test_name=function,
        ownership_hash=stubgen.file_hash(file_abs),
        status=_status_from_body(file_abs, function),
        tier=tier,
    )
    entry.resource_group = default_resource_group(manifest, entry)
    manifest.tests.append(entry)
    save_manifest(manifest, manifest_file)
    return entry
