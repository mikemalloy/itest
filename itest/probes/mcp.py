"""A probe transport for MCP servers: enumerate a server's tools, and call one.

This is the second probe transport, and it exists for the same reason the first
one does. The HTTP probe asks *does the guard hold when I knock on this URL?*;
this one asks it of an MCP server's tools, which are a far sharper edge — a
route returns data, but a tool is a named, documented invitation to run code
somebody else wrote. Every rule below follows from that:

- **Refuse to mutate by default.** A tool whose resolved mutation class is
  ``write`` or ``destructive`` is refused **before a connection is opened**
  unless the caller passes ``allow_mutating=True``. The refusal is not a policy
  the caller can forget to apply: it is the first thing :func:`call_tool` does,
  ahead of credential resolution and ahead of the transport, so a refused call
  leaves no trace on the target at all — no subprocess spawned, no request sent.
- **Sentinel arguments, always.** When mutation *is* allowed, the arguments are
  the caller's, and they must be sentinels — ids that cannot exist. A probe
  proves a guard by being **admitted**, never by destroying something. This
  module cannot inspect an argument and know it is a sentinel, so this one rule
  is the caller's to keep; everything else here is enforced.
- **Admitted-while-anonymous is CRITICAL.** An unauthenticated ``tools/call``
  that a server *answers successfully* on a mutating tool means an anonymous
  caller can mutate. That is :data:`CRITICAL`, the same class as the HTTP
  probe's unauthenticated-unsafe-2xx, and it is reported even though the
  sentinel argument means nothing was actually changed — being let in is the
  finding.
- **The credential is a NAME.** It is resolved from an env var by name through
  :func:`itest.probes.credential.resolve_credential`, never passed in, never
  stored. It is placed in an ``Authorization`` header (HTTP) or the subprocess
  environment (stdio) and nowhere else, and every string this module returns or
  raises is scrubbed of it first. A probe result gets printed, logged and pasted
  into issues; it has to be safe to.
- **One attempt, always bounded.** No retries, and the whole operation —
  connect, handshake, call — runs under :attr:`McpTarget.timeout_s`. Exceeding
  it is an ``error`` naming the limit, never a hang and never confusable with a
  refusal: a server that will not answer and a server that says no are different
  findings.
- **Redirects are not chased.** The SDK's streamable-HTTP transport follows a
  redirect only when it stays on the endpoint's origin and keeps the method, and
  it does not consult the HTTP client's ``follow_redirects``; we set that
  ``False`` regardless, so nothing this module builds will chase a 3xx to some
  other host.

Built on the official Python MCP SDK (``mcp`` on PyPI, 2.x). The protocol is the
SDK's to speak — nothing here hand-rolls a JSON-RPC frame.

What this module does **not** do: it does not read Terraform, does not touch the
manifest, and knows nothing about integration points. A detector reads Terraform
and never touches the network; a probe touches the network and never reads
Terraform. Wiring MCP tools into the manifest is later work.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import REQUEST_TIMEOUT

from itest.probes.credential import resolve_credential

#: Mutation classes that will not be called without an explicit opt-in.
MUTATING_CLASSES = frozenset({"write", "destructive"})

#: What the probe calls an anonymous caller getting a successful answer out of a
#: mutating tool. Named for the same condition the http_probe recipe classifies
#: CRITICAL, because it is the same finding.
CRITICAL = "critical"

_HASH_LENGTH = 12

# Name heuristics, used only when a tool declares no annotations. They are a
# fallback, not a truth: a server that annotates its tools is describing itself,
# and a name is just a name.
_DESTRUCTIVE_PREFIXES = ("delete_", "remove_", "purge_")
_WRITE_PREFIXES = ("create_", "update_", "set_", "link_", "unlink_")
_READ_PREFIXES = ("get_", "fetch_", "search_", "list_", "aggregate_")


class McpProbeError(Exception):
    """A probe that could not be run, or a listing that could not be obtained.

    Raised for a malformed target, an unresolvable credential, and any transport
    or protocol failure reaching :func:`list_tools` (which returns a list, so it
    has no status to report one through). Its message is always scrubbed of the
    credential before it is raised.
    """


@dataclass(frozen=True)
class ToolInfo:
    """One tool as the server declares it, plus the two drift hashes.

    ``annotations`` holds exactly the annotation fields the server sent, under
    their wire names (``readOnlyHint`` and friends), and is ``{}`` — never
    ``None`` — when it sent none. The distinction matters: a tool with no
    annotations has told you nothing, and :func:`classify_mutation` must fall
    back to its name rather than read an absent hint as a denial.

    The hashes are drift detectors and are deliberately separate.
    ``schema_hash`` covers the input schema over canonical JSON, so key ordering
    cannot move it; ``description_hash`` covers the description alone. A server
    that rewrites what a tool *says* it does without changing what it *accepts*
    moves exactly one of them, and that is a thing worth being able to see.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any]
    schema_hash: str
    description_hash: str


@dataclass(frozen=True)
class McpTarget:
    """Where an MCP server is, and how to reach it.

    ``kind="stdio"`` launches ``command`` as a subprocess and speaks JSON-RPC on
    its stdin/stdout; ``kind="http"`` connects to ``url`` over streamable HTTP.

    ``credential_env`` is the **name** of an environment variable, never a
    token. Over HTTP its value becomes a bearer ``Authorization`` header; over
    stdio it is placed in the subprocess environment under that same name, which
    is how a stdio server conventionally receives one. Either way it is supplied
    only when the caller asks for an authenticated call.

    ``timeout_s`` bounds the whole operation — spawn or connect, handshake, and
    the call itself — not just one read.
    """

    kind: Literal["stdio", "http"]
    command: list[str] | None = None
    url: str | None = None
    credential_env: str | None = None
    timeout_s: float = 10.0


@dataclass(frozen=True)
class CallResult:
    """The outcome of one ``tools/call``.

    ``status`` is the whole judgement:

    ``ok``
        The server answered, and the answer was not an error.
    ``refused``
        Nothing was called. Either the probe declined (a mutating tool without
        ``allow_mutating``) or the server declined (an authentication failure).
        Both mean the guard held, which is why they share a status.
    ``error``
        The call was made and did not succeed: a tool error result, a transport
        failure, or a timeout. The detail says which.
    ``critical``
        An unauthenticated call on a mutating tool was **admitted and answered
        successfully**. An anonymous caller can mutate.

    ``raw`` is the server's result as JSON-ready data, scrubbed of the
    credential, or ``None`` when no call was made.
    """

    ok: bool
    status: Literal["ok", "refused", "error", "critical"]
    detail: str
    raw: dict[str, Any] | None = None


# --- hashing and tool conversion ---------------------------------------------


def _canonical_json(value: Any) -> str:
    """JSON with sorted keys and no incidental whitespace.

    A schema hash must be a function of the schema's content. Two servers that
    emit the same schema with different key ordering describe the same tool, and
    a hash that disagreed would report drift that did not happen.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LENGTH]


def _tool_info(tool: Any) -> ToolInfo:
    """Convert one SDK ``Tool`` into a :class:`ToolInfo`.

    Annotations are dumped by their wire aliases with unset fields excluded, so
    what lands in ``annotations`` is exactly what the server chose to say —
    ``{}`` when it said nothing.
    """
    annotations: dict[str, Any] = {}
    if tool.annotations is not None:
        annotations = tool.annotations.model_dump(by_alias=True, exclude_none=True)

    description = tool.description or ""
    input_schema = tool.input_schema or {}
    return ToolInfo(
        name=tool.name,
        description=description,
        input_schema=input_schema,
        output_schema=tool.output_schema,
        annotations=annotations,
        schema_hash=_hash12(_canonical_json(input_schema)),
        description_hash=_hash12(description),
    )


# --- mutation classification --------------------------------------------------


def _annotation_class(annotations: dict[str, Any]) -> str | None:
    """What the server's own hints say, or ``None`` if it declared none.

    ``destructiveHint`` is only meaningful on a tool that is not read-only, so
    its mere presence is a statement that the tool writes; ``true`` sharpens
    that to destructive.
    """
    if not annotations:
        return None
    if annotations.get("destructiveHint") is True:
        return "destructive"
    if annotations.get("readOnlyHint") is True:
        return "read"
    if annotations.get("readOnlyHint") is False:
        return "write"
    if annotations.get("destructiveHint") is False:
        return "write"
    return None


def _name_class(info: ToolInfo) -> str | None:
    """What the tool's name suggests, or ``None`` if it suggests nothing.

    ``informational`` is the narrow case: a getter that takes no arguments
    cannot be pointed at anything, so calling it discloses only what the server
    volunteers. Give it a parameter and it is an ordinary read.
    """
    name = info.name.lower()
    if name.startswith(_DESTRUCTIVE_PREFIXES):
        return "destructive"
    if name.startswith(_WRITE_PREFIXES):
        return "write"
    if name.startswith(_READ_PREFIXES):
        has_params = bool((info.input_schema or {}).get("properties"))
        if name.startswith("get_") and not has_params:
            return "informational"
        return "read"
    return None


def classify_mutation(info: ToolInfo) -> tuple[str, str]:
    """Return ``(class, source)`` for a tool: what calling it probably does.

    ``class`` is one of ``read``, ``write``, ``destructive``, ``informational``,
    or ``unknown``. ``source`` says what decided:

    ``annotation``
        The server's own hints, which win whenever they exist.
    ``name``
        The name heuristic, used only when there are no hints.
    ``conflict:name-says-<x>``
        The hints decided **and** the name disagreed. The returned class is
        still the annotation's answer — it is the server describing itself — but
        the disagreement is *reported rather than resolved*, because one of the
        two is wrong and this function cannot know which. A ``delete_*`` tool
        declaring itself read-only is either mis-annotated or mis-named, and
        either way somebody should look.
    ``unknown``
        Neither decided.

    A word on what this cannot do. Both inputs are *declarations* — what the
    server says about itself. A tool whose annotation and name agree with each
    other and both misdescribe its behaviour looks perfectly consistent here,
    and only calling it and watching what changes would reveal otherwise.
    ``lookalike_read`` in ``examples/reference-mcp/`` is exactly that shape, and
    it is deliberately not a conflict: catching it is a different job from this
    one.
    """
    annotation = _annotation_class(info.annotations)
    name = _name_class(info)
    if annotation is not None:
        if name is not None and name != annotation:
            return annotation, f"conflict:name-says-{name}"
        return annotation, "annotation"
    if name is not None:
        return name, "name"
    return "unknown", "unknown"


# --- credentials and scrubbing ------------------------------------------------


def _scrub(text: str, secret: str | None) -> str:
    """Remove the credential from anything on its way out of this module.

    Defence in depth, not the primary control: the probe never puts the token in
    a URL or a message itself. But a server can echo it back, and an SDK
    exception can quote a request, so every string returned or raised passes
    through here first.
    """
    if not secret:
        return text
    return text.replace(secret, "***")


def _resolve(
    target: McpTarget, authenticated: bool, base_dir: Path | None
) -> str | None:
    """The credential for this call, or ``None`` when none is wanted.

    An authenticated call whose credential cannot be resolved is a usage error,
    not a quiet fallback to anonymous: probing anonymously while reporting an
    authenticated result would be a lie about what was verified. The error names
    the env var — a name is safe to print, which is the whole point of naming
    one instead of holding a token.
    """
    if not authenticated:
        return None
    if not target.credential_env:
        raise McpProbeError(
            "authenticated=True but the target names no credential_env. The "
            "probe reads a credential by env-var NAME; it has no other source."
        )
    value = resolve_credential(target.credential_env, base_dir)
    if value is None:
        raise McpProbeError(
            f"authenticated=True but {target.credential_env} is unset or empty. "
            "Export it, or put it in a gitignored .itest/.env as "
            f"{target.credential_env}=<token>."
        )
    return value


# --- transport ----------------------------------------------------------------


def _client(target: McpTarget, credential: str | None) -> tuple[Client, list[int]]:
    """Build a client for the target, plus the list its HTTP statuses land in.

    The status list is how an authentication refusal is told from a transport
    failure: the SDK surfaces a 401 as a generic protocol error, so the status
    code has to be observed as it goes past. It stays empty for stdio, which has
    no such thing.
    """
    statuses: list[int] = []
    if target.kind == "http":
        if not target.url:
            raise McpProbeError("an http target needs a url")
        headers = {}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"

        async def record(response: httpx2.Response) -> None:
            statuses.append(response.status_code)

        http = httpx2.AsyncClient(
            headers=headers,
            timeout=target.timeout_s,
            # The SDK applies its own same-origin-only redirect rule and does
            # not consult this; set it anyway so nothing built here can chase a
            # 3xx to another host.
            follow_redirects=False,
            event_hooks={"response": [record]},
        )
        transport = streamable_http_client(target.url, http_client=http)
        return Client(transport, read_timeout_seconds=target.timeout_s), statuses

    if target.kind == "stdio":
        if not target.command:
            raise McpProbeError("a stdio target needs a command")
        # The subprocess inherits this process's environment plus the credential
        # under its recorded name, and only when one was resolved.
        env = dict(os.environ)
        env.pop(target.credential_env or "", None)
        if credential and target.credential_env:
            env[target.credential_env] = credential
        params = StdioServerParameters(
            command=target.command[0], args=list(target.command[1:]), env=env
        )
        return Client(params, read_timeout_seconds=target.timeout_s), statuses

    raise McpProbeError(f"unknown target kind {target.kind!r}: use 'stdio' or 'http'")


def _leaves(exc: BaseException) -> list[BaseException]:
    """Flatten an exception group into its leaves.

    The SDK runs its transports in task groups, so a 401 arrives wrapped in two
    layers of ``ExceptionGroup``. The interesting exception is always a leaf.
    """
    if isinstance(exc, BaseExceptionGroup):
        found: list[BaseException] = []
        for inner in exc.exceptions:
            found.extend(_leaves(inner))
        return found
    return [exc]


def _describe(exc: BaseException, statuses: list[int]) -> tuple[str, str]:
    """Classify a failed operation as ``(status, detail)``.

    An HTTP 401/403 is a *refusal* — the guard held, which is the good outcome —
    and must not be filed alongside "the server was not there". A request
    timeout is an error that says so, so that a hang is never mistaken for a no.
    """
    if any(code in (401, 403) for code in statuses):
        code = next(c for c in statuses if c in (401, 403))
        return "refused", f"the server refused the connection with HTTP {code}"

    for leaf in _leaves(exc):
        code = getattr(leaf, "code", None)
        if code == REQUEST_TIMEOUT:
            return "error", str(leaf)
    summary = (
        "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in _leaves(exc))
        or f"{type(exc).__name__}: {exc}"
    )
    return "error", summary


# --- the public operations ----------------------------------------------------


def list_tools(target: McpTarget, *, base_dir: Path | None = None) -> list[ToolInfo]:
    """Enumerate a server's tools.

    Uses the target's credential when one is named and resolvable, and connects
    anonymously otherwise — enumerating an MCP server without a credential is
    itself a reasonable thing to ask, and an open server answering it is a
    finding worth being able to observe.

    Raises :class:`McpProbeError` on any failure, scrubbed of the credential. It
    never returns an empty list to mean "could not connect": an empty list is a
    server with no tools, which is a different fact.
    """
    credential = None
    if target.credential_env:
        credential = resolve_credential(target.credential_env, base_dir)
    client, statuses = _client(target, credential)

    async def run() -> list[ToolInfo]:
        with anyio.fail_after(target.timeout_s):
            async with client as connected:
                listing = await connected.list_tools()
                return [_tool_info(tool) for tool in listing.tools]

    try:
        return anyio.run(run)
    except TimeoutError:
        raise McpProbeError(
            _scrub(f"listing tools timed out after {target.timeout_s}s", credential)
        ) from None
    except BaseException as exc:  # noqa: BLE001 - re-raised as a scrubbed error
        _, detail = _describe(exc, statuses)
        if any(code in (401, 403) for code in statuses):
            raise McpProbeError(
                _scrub(f"the server refused the tool listing: {detail}", credential)
            ) from None
        raise McpProbeError(
            _scrub(f"could not list tools: {detail}", credential)
        ) from None


def call_tool(
    target: McpTarget,
    name: str,
    arguments: dict[str, Any],
    *,
    authenticated: bool,
    allow_mutating: bool = False,
    mutation_class: str | None = None,
    base_dir: Path | None = None,
) -> CallResult:
    """Call one tool once, under every safety rule in the module docstring.

    ``mutation_class`` is the caller's answer to "what does this tool do?",
    normally :func:`classify_mutation`'s. When it is ``write`` or
    ``destructive`` the call is **refused before anything is opened** unless
    ``allow_mutating=True``; the refusal comes first, ahead of the credential
    and the transport, so nothing reaches the target. ``None`` and ``unknown``
    are not gated: gating them would refuse most of a real server, and whether
    an unclassifiable tool is safe to call is the caller's judgement, not this
    module's guess.

    When mutation is allowed, **the arguments must be sentinels** — ids that
    cannot exist. This is the one rule here that cannot be enforced: an argument
    is opaque to this module. A probe proves a guard by being admitted, not by
    destroying something.

    Never raises for anything the server did — a refusal, a tool error and a
    timeout are all outcomes, and telling them apart is the caller's whole
    reason for probing. :class:`McpProbeError` is raised only for a target or a
    credential the probe cannot use at all.
    """
    resolved_class = (mutation_class or "unknown").strip().lower()
    if resolved_class in MUTATING_CLASSES and not allow_mutating:
        # First, before the credential and before the transport: a refused call
        # must leave no trace on the target.
        return CallResult(
            ok=False,
            status="refused",
            detail=(
                f"refused to call {name!r}: its mutation class is "
                f"{resolved_class!r} and allow_mutating is False. Mutating tools "
                "are called only with sentinel arguments and only on explicit "
                "opt-in."
            ),
            raw=None,
        )

    credential = _resolve(target, authenticated, base_dir)
    client, statuses = _client(target, credential)

    async def run() -> Any:
        with anyio.fail_after(target.timeout_s):
            async with client as connected:
                return await connected.call_tool(name, arguments)

    try:
        result = anyio.run(run)
    except TimeoutError:
        return CallResult(
            ok=False,
            status="error",
            detail=_scrub(
                f"calling {name!r} timed out after {target.timeout_s}s", credential
            ),
            raw=None,
        )
    except BaseException as exc:  # noqa: BLE001 - every failure is an outcome here
        status, detail = _describe(exc, statuses)
        return CallResult(
            ok=False,
            status=status,  # type: ignore[arg-type]
            detail=_scrub(f"calling {name!r}: {detail}", credential),
            raw=None,
        )

    return _judge(name, result, credential, authenticated, resolved_class)


def _judge(
    name: str,
    result: Any,
    credential: str | None,
    authenticated: bool,
    resolved_class: str,
) -> CallResult:
    """Turn a server's answer into a status.

    The CRITICAL rule reads the *success* of the answer, not its content: an
    unauthenticated caller who gets a real result out of a mutating tool got in.
    A tool-level error on the same call is still admission — the detail says so
    — but it is not filed as CRITICAL, because a server that answered "no such
    record" may equally be one that checked the caller and refused inside the
    tool. Guessing which would either invent an emergency or hide one; naming
    what happened does neither.
    """
    raw = json.loads(_scrub(json.dumps(result.model_dump(mode="json")), credential))
    admitted_anonymously = not authenticated and resolved_class in MUTATING_CLASSES

    if result.is_error:
        detail = _scrub(_result_text(result), credential)
        if admitted_anonymously:
            detail = (
                f"an unauthenticated call of the {resolved_class} tool {name!r} was "
                f"ADMITTED and answered with a tool error: {detail}. The guard did "
                "not stop the call reaching the tool."
            )
        return CallResult(ok=False, status="error", detail=detail, raw=raw)

    if admitted_anonymously:
        return CallResult(
            ok=False,
            status=CRITICAL,
            detail=(
                f"CRITICAL: an unauthenticated call of the {resolved_class} tool "
                f"{name!r} was admitted and answered successfully. An anonymous "
                "caller can mutate. The arguments were sentinels, so nothing was "
                "changed to learn this — being let in is the finding."
            ),
            raw=raw,
        )

    return CallResult(
        ok=True,
        status="ok",
        detail=f"{name!r} answered successfully",
        raw=raw,
    )


def _result_text(result: Any) -> str:
    """The text a server put in a result, for a human-readable detail."""
    parts = [getattr(block, "text", "") for block in (result.content or [])]
    return " ".join(part for part in parts if part) or "(no content)"
