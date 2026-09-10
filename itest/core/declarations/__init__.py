"""Declarations: turning a declared MCP server into ITest points.

Ring 3 of the design. A detector reads Terraform and infers points; a
declaration is the other half — a server nobody can infer from state, described
by the person who runs it, in ``.itest/tools/<server>.yaml``.

The contract, in one line: **facts in, traits out.** The file states how to reach
the server, which environment variables hold its url and tokens, what a
non-existent id looks like, where mutations are audited. ITest decides the rest:
the mutation class is *detected* from the live tool listing and only
cross-checked against a declared one, and which checks a tool gets comes from
the applies-when table in ``itest/traits/traits.yaml`` — data, not code.

- :mod:`itest.core.declarations.schema` — what may be stated.
- :mod:`itest.core.declarations.loader` — reading and refusing files.
"""

from __future__ import annotations

from itest.core.declarations.loader import (
    DeclarationError,
    declaration_path,
    declarations_dir,
    load_declarations,
    missing_url_message,
    resolve_url,
)
from itest.core.declarations.schema import (
    NO_TRAITS,
    ApprovalRule,
    Auth,
    Declaration,
    DeclaredMutation,
    Defaults,
    Egress,
    MutationClass,
    ToolOverride,
    Transport,
)

__all__ = [
    "NO_TRAITS",
    "ApprovalRule",
    "Auth",
    "Declaration",
    "DeclarationError",
    "DeclaredMutation",
    "Defaults",
    "Egress",
    "MutationClass",
    "ToolOverride",
    "Transport",
    "declaration_path",
    "declarations_dir",
    "load_declarations",
    "missing_url_message",
    "resolve_url",
]
