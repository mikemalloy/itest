"""Active probes: the code an active-tier test runs to touch a live endpoint.

Probes live apart from the detectors and the CLI on purpose. A detector reads
Terraform and never touches the network; a probe touches the network and never
reads Terraform. Keeping them in separate packages means the read-only analysis
path has no accidental route to sending a request.

Two transports ship:

``http``
    A single-shot HTTP probe. Bodyless by construction, follows no redirect,
    never retries, and adds no authorization of its own.
``mcp``
    An MCP *client*: it connects to somebody else's MCP server over stdio or
    streamable HTTP, enumerates its tools, and calls one. It refuses to call a
    mutating tool unless the caller explicitly opts in, and classifies an
    unauthenticated mutating call the server admits as CRITICAL.

Both resolve a credential by env-var **name** through
:mod:`itest.probes.credential` and never store, log, or echo the value.
"""
