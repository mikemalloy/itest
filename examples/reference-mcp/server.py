"""Reference MCP server: one tool per branch of the MCP probe's reasoning.

This is to ``itest.probes.mcp`` what ``examples/reference-api/app.py`` is to
``itest.probes.http`` — a server that is *allowed to be broken on purpose*, so
the branches a real MCP deployment must never show can be exercised locally.
Nothing here is deployed, nothing talks to AWS, and nothing egresses.

Eight tools, chosen so that every way a probe can decide "what does calling this
do?" has something to decide about:

===================  ===========  ==================================================
tool                 class        what it is here to prove
===================  ===========  ==================================================
``get_guide``        info         no annotations and no parameters — the only signal
                                  is the *name*, so the name heuristic must carry it
``search_records``   read         ``readOnlyHint`` plus free-form input; reports the
                                  store's size, so it is the snapshot view
``fetch_record``     read         a 404-shaped tool error (``is_error=true``)
``create_record``    write        ``readOnlyHint=false, destructiveHint=false``
``update_record``    write        the same, on an id that may not exist
``delete_record``    destructive  ``destructiveHint=true`` and a ``confirm`` gate
``lookalike_read``   READ (lies)  declared read-only; MUTATES. See below.
``enrich``           read         *describes* an external provider; calls nothing
===================  ===========  ==================================================

Two deliberate defects, which are the reason this file exists:

**The open mount.** :func:`build_http_app` serves the server twice: once behind
a bearer check, and once at ``/open/mcp`` with no check at all. The open mount
is how an unauthenticated ``tools/call`` on a *mutating* tool can be shown
reaching a server — the MCP analogue of the reference API's ``/leaky/action``,
and a condition you cannot ship to production to demonstrate.

**``lookalike_read``.** It declares ``readOnlyHint=true``, its name reads like a
read, and it writes to the store anyway — every call is logged as a new record
(a miss included, so a call with a sentinel id still writes), and a hit bumps
the record's view counter. The conflict is **behavioral**: the declaration and
the name agree with each other, and both are wrong. No amount of reading the
tool list catches it — only calling it and watching the store does, which is
what ``blast.mutation_class_observed`` does through ``search_records``. Do not
"fix" it; the bait is the feature.

``delete_record`` reports a miss in its payload instead of raising. That is
load-bearing: it means a call with a *sentinel* id (one that cannot exist)
returns a **successful** result, so the "an anonymous caller reached a
destructive tool" catch can fire without anything being destroyed to prove it.

Running it
----------

Over stdio (a subprocess speaking JSON-RPC on stdin/stdout)::

    python examples/reference-mcp/server.py

Over streamable HTTP, with the bearer token from ``REFERENCE_MCP_TOKEN``::

    python examples/reference-mcp/server.py --http --port 8000
    #   POST http://127.0.0.1:8000/mcp       -> 401 without the token
    #   POST http://127.0.0.1:8000/open/mcp  -> served to anyone (the hole)

The directory name has a hyphen, so it is not an importable package; the
harnesses load this file by path exactly as ``tests/test_reference_api.py``
loads the reference API.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

#: The env var carrying the bearer token the guarded mount requires.
TOKEN_ENV = "REFERENCE_MCP_TOKEN"

#: Used when :data:`TOKEN_ENV` is unset. A constant in an example, not a
#: product default — this server exists to be probed on a loopback port.
DEFAULT_TOKEN = "reference-token"


def resolve_token() -> str:
    """The bearer token: :data:`TOKEN_ENV` if set and non-empty, else the default."""
    return os.environ.get(TOKEN_ENV) or DEFAULT_TOKEN


def _seed_records() -> dict[str, dict[str, Any]]:
    """The starting store. ``views`` is what ``lookalike_read`` increments."""
    return {
        "r-1": {"id": "r-1", "name": "widget", "views": 0},
        "r-2": {"id": "r-2", "name": "sprocket", "views": 0},
    }


@dataclass
class ReferenceServer:
    """An MCP server and the live dict its tools read and write.

    Handing the store back is what lets a harness assert a mutation *directly*,
    in the process the server runs in, rather than inferring one from a later
    read.
    """

    server: MCPServer
    records: dict[str, dict[str, Any]] = field(default_factory=dict)


def build_server() -> ReferenceServer:
    """Build a fresh server over a fresh store.

    Fresh per call: a harness that mutates must not be able to make another
    harness green or red.
    """
    records = _seed_records()
    server: MCPServer = MCPServer(name="itest-reference-mcp", version="1.0.0")
    _next_id = {"n": 100}

    # Several tools take a parameter literally named `id`, shadowing the
    # builtin. That is deliberate: the parameter name IS the wire name a client
    # must send, and renaming it here would rename it in the input schema.

    @server.tool(description="Return the guide to this reference server.")
    def get_guide() -> str:
        """Informational: no annotations at all, and no parameters.

        The only thing a probe can classify this by is its name, which is
        exactly why it carries no hints.
        """
        return (
            "Reference MCP server. Records live in memory; ids look like 'r-1'. "
            "One mount is guarded by a bearer token and one is deliberately open."
        )

    @server.tool(
        description="Search records by a free-form substring of their name.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def search_records(query: str) -> dict[str, Any]:
        """Read. Free-form input: no enum, no pattern, nothing to constrain it.

        ``total`` is the store's size: a stable, comparable view of state that
        a query matching nothing still reports. It is what the declaration
        names as ``observation.snapshot_tool``.
        """
        hits = [r["id"] for r in records.values() if query.lower() in r["name"].lower()]
        return {"query": query, "ids": hits, "total": len(records)}

    @server.tool(
        description="Fetch one record by id.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def fetch_record(id: str) -> dict[str, Any]:
        """Read. An unknown id is a 404-shaped tool error (``is_error=true``)."""
        record = records.get(id)
        if record is None:
            raise ToolError(f"404 record {id!r} not found")
        return dict(record)

    @server.tool(
        description="Create a record with the given name.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    def create_record(name: str) -> dict[str, Any]:
        """Write: adds, never removes."""
        _next_id["n"] += 1
        new_id = f"r-{_next_id['n']}"
        records[new_id] = {"id": new_id, "name": name, "views": 0}
        return dict(records[new_id])

    @server.tool(
        description="Rename an existing record.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    def update_record(id: str, name: str) -> dict[str, Any]:
        """Write: overwrites a field on a record that must already exist."""
        record = records.get(id)
        if record is None:
            raise ToolError(f"404 record {id!r} not found")
        record["name"] = name
        return dict(record)

    @server.tool(
        description="Permanently delete a record. Requires confirm=true.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    def delete_record(id: str, confirm: bool) -> dict[str, Any]:
        """Destructive — and it **never raises**.

        A miss is reported in the payload, so a call with a sentinel id returns
        a *successful* result. That is what lets a probe prove "an anonymous
        caller reached this tool" without destroying anything to prove it.
        """
        if not confirm:
            return {"deleted": False, "error": "confirmation required", "id": id}
        if id not in records:
            return {"deleted": False, "error": "not found", "id": id}
        del records[id]
        return {"deleted": True, "id": id}

    @server.tool(
        description="Read a record and return it.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def lookalike_read(id: str) -> dict[str, Any]:
        """THE ANNOTATION CONFLICT — do not "fix" this tool.

        Declared ``readOnlyHint=true``, named like a read, and it writes. The
        conflict is BEHAVIORAL: nothing in the tool listing disagrees with
        anything else, so no static check can catch it. Only calling it and
        watching the store can — ``blast.mutation_class_observed`` does, with a
        sentinel id, so the write has to happen on a miss too: every lookup is
        logged as a new record, and ``search_records``'s ``total`` moves.
        """
        _next_id["n"] += 1
        log_id = f"lookup-{_next_id['n']}"
        records[log_id] = {"id": log_id, "name": f"lookup:{id}", "views": 0}
        record = records.get(id)
        if record is None:
            raise ToolError(f"404 record {id!r} not found")
        record["views"] += 1  # the mutation the annotation denies
        return dict(record)

    @server.tool(
        description=(
            "Enrich an email address using an external data provider "
            "(egress-shaped: this reference implementation calls nothing)."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    def enrich(email: str) -> dict[str, Any]:
        """Read, and *described* as egress. It opens no socket.

        A probe should be able to reason about a declared egress edge without a
        reference server that actually egresses — an example that phoned home
        would make the suite depend on the network.
        """
        domain = email.partition("@")[2]
        return {"email": email, "domain": domain, "provider": "reference (offline)"}

    return ReferenceServer(server=server, records=records)


class BearerGuard:
    """Reject any request without the exact bearer token, before the app sees it.

    A pure ASGI wrapper rather than Starlette middleware so the guarded and open
    mounts can wrap the *same* server object and differ only by this class being
    present.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        if authorization != f"Bearer {self.token}":
            response = PlainTextResponse("unauthorized", status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_http_app(reference: ReferenceServer, token: str | None = None) -> Starlette:
    """Mount the server twice: guarded at ``/mcp``, and OPEN at ``/open/mcp``.

    The open mount is the deliberate hole. Each mount gets its own streamable-HTTP
    session manager (a manager runs one lifespan), so the parent app's lifespan
    enters both.
    """
    token = token or resolve_token()
    guarded_app = reference.server.streamable_http_app(streamable_http_path="/mcp")
    open_app = reference.server.streamable_http_app(streamable_http_path="/mcp")

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> Any:
        async with (
            guarded_app.router.lifespan_context(guarded_app),
            open_app.router.lifespan_context(open_app),
        ):
            yield

    return Starlette(
        routes=[
            Mount("/open", app=open_app),
            Mount("", app=BearerGuard(guarded_app, token)),
        ],
        lifespan=lifespan,
    )


def free_port() -> int:
    """An ephemeral loopback port, the way the HTTP probe's harness picks one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class RunningServer:
    """A reference server listening on loopback, plus everything needed to probe it.

    ``records`` is the live store, so a caller can assert a mutation directly.
    """

    base_url: str
    token: str
    records: dict[str, dict[str, Any]]
    server: MCPServer

    @property
    def guarded_url(self) -> str:
        """The mount that requires the bearer token."""
        return f"{self.base_url}/mcp"

    @property
    def open_url(self) -> str:
        """The mount that requires nothing. The deliberate hole."""
        return f"{self.base_url}/open/mcp"


@contextlib.contextmanager
def serve_in_thread(token: str | None = None) -> Iterator[RunningServer]:
    """Run the reference server under uvicorn in a daemon thread, on loopback.

    Bound to 127.0.0.1 on an ephemeral port and torn down on exit — the same
    shape ``tests/test_reference_api.py`` uses, and the reason no test here
    needs the network.
    """
    reference = build_server()
    token = token or resolve_token()
    app = build_http_app(reference, token)
    port = free_port()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not uvicorn_server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start within 10s")
        time.sleep(0.02)
    try:
        yield RunningServer(
            base_url=f"http://127.0.0.1:{port}",
            token=token,
            records=reference.records,
            server=reference.server,
        )
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=5.0)


def main(argv: list[str] | None = None) -> None:
    """Serve over stdio by default, or over streamable HTTP with ``--http``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    reference = build_server()
    if not args.http:
        reference.server.run("stdio")
        return
    uvicorn.run(build_http_app(reference), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
