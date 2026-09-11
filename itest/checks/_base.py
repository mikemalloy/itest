"""What every check shares: the result type, the registry, scrubbing, and the
per-run caches of live listings and project files.

Private to the package. The public surface is :mod:`itest.checks` — the result
type, the two ``run_*`` entry points and ``clear_cache`` — and this module is
where they are implemented so the check modules can import them without a
circular import through the package ``__init__``.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from itest.core import redact

# ``_load_one`` validates one declaration file against the schema without the
# environment-policy cross-check ``load_declarations`` adds. A check reads the
# declaration for its sentinels only, and the policy has nothing to say about
# those; importing the private loader is one reader, where copying its parsing
# would be two.
from itest.core.declarations.loader import (
    DeclarationError,
    _load_one,
    declarations_dir,
)
from itest.core.declarations.schema import Declaration
from itest.core.manifest import load_manifest
from itest.core.planner import manifest_path
from itest.probes.credential import CredentialError, resolve_credential
from itest.probes.mcp import McpProbeError, McpTarget, ToolInfo, list_tools

#: Every status a check may return.
STATUSES = frozenset({"pass", "fail", "critical", "changed", "not_verifiable"})


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one trait's check for one tool.

    ``status`` is one of :data:`STATUSES`. ``detail`` is one human-readable line
    and never a credential. ``evidence`` is JSON-ready and is what the report may
    show; ``None`` when there is nothing beyond the detail.
    """

    status: str  # "pass" | "fail" | "critical" | "changed" | "not_verifiable"
    detail: str
    evidence: dict | None = None


EngineCheck = Callable[..., CheckResult]

#: trait id -> engine check. Populated by :func:`engine_check` as the check
#: modules are imported by the package ``__init__``.
ENGINE_CHECKS: dict[str, EngineCheck] = {}

#: trait id -> generated check. Empty until the A2/B2/B4 recipes ship; the
#: registry exists so ``run_generated_check`` has something to dispatch on.
GENERATED_CHECKS: dict[str, Callable[..., CheckResult]] = {}


def not_verifiable(detail: str, evidence: dict | None = None) -> CheckResult:
    return CheckResult(status="not_verifiable", detail=detail, evidence=evidence)


# --- scrubbing ----------------------------------------------------------------


def _credential_values(target: McpTarget) -> list[str]:
    """The secret values this result must not carry: the target's credential."""
    if not target.credential_env:
        return []
    try:
        value = resolve_credential(target.credential_env, Path.cwd())
    except CredentialError:
        return []
    return [value] if value else []


def scrub_result(result: CheckResult, target: McpTarget) -> CheckResult:
    """Every string in ``result`` — detail, evidence keys and values — scrubbed.

    Two passes, both existing scrubbers: the credential's own value is replaced
    with ``***`` (the MCP probe's rule, applied here because a server can echo a
    token into an error the probe did not know was secret on an anonymous call),
    then :func:`itest.core.redact.text_scrubber` removes credential-shaped text
    and pseudonymizes account ids. A ``*_hash`` value skips the second pass
    only: a twelve-digit drift hash is data, and pseudonymizing it as an account
    id would corrupt the evidence D2 and D3 exist to show.
    """
    secrets = _credential_values(target)
    patterns = redact.text_scrubber()

    def text(value: str, *, keep_shape: bool = False) -> str:
        for secret in secrets:
            value = value.replace(secret, "***")
        return value if keep_shape else patterns(value)

    def walk(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            return text(value, keep_shape=key.endswith("_hash"))
        if isinstance(value, dict):
            return {text(str(k)): walk(v, str(k)) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [walk(v, key) for v in value]
        return value

    evidence = walk(result.evidence) if result.evidence is not None else None
    return dataclasses.replace(result, detail=text(result.detail), evidence=evidence)


def engine_check(trait_id: str) -> Callable[[EngineCheck], EngineCheck]:
    """Register an engine check under ``trait_id``, scrubbed and exception-safe.

    The wrapper is what makes every result safe to print whichever way the
    check is reached — through ``run_engine_check`` or called directly — and
    turns a target the probe cannot use at all (no url, a refused private host,
    an unreadable ``.itest/.env``) into ``not_verifiable`` rather than an
    exception: one unusable server must not stop the rest of a verify run.
    """

    def register(check: EngineCheck) -> EngineCheck:
        @functools.wraps(check)
        def wrapped(point: dict, target: McpTarget, *, authenticated: bool):
            try:
                result = check(point, target, authenticated=authenticated)
            except (McpProbeError, CredentialError) as exc:
                result = not_verifiable(f"{trait_id} could not run: {exc}")
            return scrub_result(result, target)

        ENGINE_CHECKS[trait_id] = wrapped
        return wrapped

    return register


# --- the point ----------------------------------------------------------------


def server_of(point: dict) -> str:
    """The server a point belongs to. ``server`` in the contract; ``source`` is
    what the manifest itself calls it, accepted so a raw manifest dict works."""
    return str(point.get("server") or point.get("source") or "")


def tool_of(point: dict) -> str:
    return str(point.get("target") or "")


def attributes_of(point: dict) -> dict:
    return dict(point.get("attributes") or {})


# --- the per-run cache --------------------------------------------------------

#: (target key, anonymous) -> the listing, or the error that listing raised.
_LISTINGS: dict[tuple, list[ToolInfo] | McpProbeError] = {}
#: server -> the tool names the manifest records for it, or None (no manifest).
_MANIFEST_TOOLS: dict[str, list[str] | None] = {}
#: server -> its declaration, or the reason there is none.
_DECLARATIONS: dict[str, Declaration | str] = {}


def clear_cache() -> None:
    """Forget every cached listing, manifest read and declaration.

    Call once at the start of each verify run. Within a run a server is listed
    once per mode (authenticated, anonymous), however many tools and traits ask.
    """
    _LISTINGS.clear()
    _MANIFEST_TOOLS.clear()
    _DECLARATIONS.clear()


def _target_key(target: McpTarget) -> tuple:
    # McpTarget is frozen but holds a list, so it is not hashable itself.
    return (
        target.kind,
        tuple(target.command or ()),
        target.url,
        target.credential_env,
        target.timeout_s,
        target.allow_private_hosts,
        target.cwd,
    )


class ListingUnavailable(Exception):
    """A live listing could not be obtained. The message is the reason."""

    def __init__(self, message: str, *, refused: bool = False) -> None:
        super().__init__(message)
        self.refused = refused


def live_listing(target: McpTarget, *, anonymous: bool) -> list[ToolInfo]:
    """One ``tools/list`` per target and mode per run. Raises
    :class:`ListingUnavailable` (cached too, so a dead server is asked once)."""
    key = (_target_key(target), anonymous)
    if key not in _LISTINGS:
        try:
            _LISTINGS[key] = list_tools(
                target, base_dir=Path.cwd(), anonymous=anonymous
            )
        except McpProbeError as exc:
            _LISTINGS[key] = exc
    cached = _LISTINGS[key]
    if isinstance(cached, McpProbeError):
        raise ListingUnavailable(str(cached), refused=cached.refused)
    return cached


def listing_label(authenticated: bool) -> str:
    """How the reference listing was taken, for ``evidence.listing``."""
    return "authenticated" if authenticated else "anonymous"


def reference_listing(target: McpTarget, *, authenticated: bool) -> list[ToolInfo]:
    """The listing a check compares the manifest against.

    ``authenticated=False`` — no credential resolves, the normal case for a stdio
    server like reference-mcp — takes the **anonymous** listing, and the checks
    run on it when the server admits one. Only a refused anonymous listing is
    unavailable, and its reason names the variable that would unlock the
    authenticated one (a name, never a value).

    ``authenticated=True`` takes it with the target's credential and refuses to
    fall back to anonymous when the credential cannot be resolved — reporting an
    anonymous listing as an authenticated one would misstate what was verified.
    A target that names no credential has nothing to authenticate with, so its
    listing is simply the one the server gives.
    """
    if authenticated and target.credential_env:
        if not resolve_credential(target.credential_env, Path.cwd()):
            raise ListingUnavailable(
                f"an authenticated listing was asked for, but {target.credential_env}"
                " is unset or empty"
            )
    try:
        return live_listing(target, anonymous=not authenticated)
    except ListingUnavailable as exc:
        if authenticated or not exc.refused:
            raise
        unlock = (
            f"export {target.credential_env} to take the authenticated listing"
            if target.credential_env
            else "the declaration names no credential that could unlock it"
        )
        raise ListingUnavailable(
            f"the server refused the anonymous tool listing ({exc}); {unlock}",
            refused=True,
        ) from None


def find_tool(tools: list[ToolInfo], name: str) -> ToolInfo | None:
    for tool in tools:
        if tool.name == name:
            return tool
    return None


def manifest_tools(server: str) -> list[str] | None:
    """The tool names the project manifest records for ``server``.

    ``None`` — not ``[]`` — when there is no manifest to read: "no tools
    recorded" and "nothing to compare against" are different facts.
    """
    if server not in _MANIFEST_TOOLS:
        path = manifest_path(Path.cwd())
        if not path.exists():
            _MANIFEST_TOOLS[server] = None
        else:
            manifest = load_manifest(path)
            _MANIFEST_TOOLS[server] = sorted(
                p.target
                for p in manifest.points
                if p.type == "mcp_tool" and p.source == server
            )
    return _MANIFEST_TOOLS[server]


def declaration_for(server: str) -> Declaration:
    """The server's declaration from the project root, cached per run.

    Raises :class:`DeclarationMissing`, naming the reason, when the file is absent
    or does not validate.
    """
    if server not in _DECLARATIONS:
        path = declarations_dir(Path.cwd()) / f"{server}.yaml"
        if not path.exists():
            _DECLARATIONS[server] = f"{path.relative_to(Path.cwd())} not found"
        else:
            try:
                _DECLARATIONS[server] = _load_one(path)
            except DeclarationError as exc:
                _DECLARATIONS[server] = str(exc)
    cached = _DECLARATIONS[server]
    if isinstance(cached, str):
        raise DeclarationMissing(cached)
    return cached


class DeclarationMissing(Exception):
    """No usable declaration for a server. The message is the reason."""
