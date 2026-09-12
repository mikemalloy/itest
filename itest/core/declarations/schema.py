"""The declaration schema: what a developer may state about an MCP server.

A declaration (``.itest/tools/<server>.yaml``) is the input side of Ring 3. It
holds **facts** — how to reach the server, which environment variable holds its
token, what a non-existent id looks like, where a mutation is audited — and the
schema is shaped so that the one thing a developer cannot state is the one thing
a developer should not be trusted to state:

- **The dangerous field is never declared.** ``defaults.mutation`` defaults to
  ``detect``. A real class *may* be written, per server or per tool, but it is
  only ever cross-checked against what the live tool listing says; a
  disagreement refuses generation rather than picking a winner. There is no
  ``dangerous: true`` field here and there never will be.
- **A URL and a credential are NAMES.** ``transport.url_env`` and
  ``auth.credential_env`` name environment variables. A value that looks like a
  URL or a token is refused, and the refusal never quotes it: a declaration is
  committed to the repository and its errors are pasted into issues.
- **Unknown keys are errors**, at every level. A typo in a declaration would
  otherwise be a fact silently not stated, which is indistinguishable from a
  fact deliberately withheld.
- **Absence grants nothing.** Every optional section defaults to its *least*
  claim: no auth, no audit sink, no approval requirement, no active environment,
  egress none. A forgotten section withholds a check; it never loosens one.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from itest.traits.ids import migrate_trait_id

#: What calling a tool does. The vocabulary the MCP probe's classifier returns,
#: minus its ``unknown``: a declaration states a belief, and "I don't know" is
#: spelled ``detect``.
MutationClass = Literal["read", "write", "destructive", "informational"]

#: What a declaration may write for a mutation class. ``detect`` is the default
#: and the honest answer for a server you did not write.
DeclaredMutation = Literal["detect", "read", "write", "destructive", "informational"]

#: How a destructive call is gated. ``none`` is a statement, not a gap.
ApprovalRule = Literal["none", "confirm_param", "human", "policy"]

#: Spelled out in a ``traits:`` list to say a tool needs no checks at all. It
#: requires ``notes``: the claim is a person's, and it has to be signed.
NO_TRAITS = "none-of-these"

#: A server name: the file name, a point id input, and a stub name fragment.
_SERVER_NAME = re.compile(r"^[a-z0-9-]+$")

#: An environment variable name. Deliberately narrow: anything with a ``:``, a
#: ``/`` or a space is a URL or a token that has been pasted where a name goes.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A trait id as the traits table spells them: ``<family>.<slug>``. An old
#: AN-style id (``A1``) is accepted and read as the slug it was renamed to.
_TRAIT_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def _env_name(value: str | None, field: str) -> str | None:
    """Validate an env-var NAME without ever repeating its value.

    The value is the thing being guarded — a URL or a token pasted into the file
    — so the message names the field and the rule and stops there.
    """
    if value is None:
        return None
    if not _ENV_NAME.match(value):
        raise ValueError(
            f"{field} must be the NAME of an environment variable "
            "(letters, digits and underscores), not the value itself. ITest "
            "never stores a URL or a credential in a declaration: it resolves "
            "the name from the environment at plan time and never logs it. The "
            "offending value is not repeated here on purpose."
        )
    return value


class Strict(BaseModel):
    """Base for every declaration model: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


class Egress(Strict):
    """A declared edge out of the server: where data goes, and which data."""

    #: The destination, as a host or a service name. A fact, not a secret.
    to: str
    #: Which declared arguments leave. Empty means "unspecified", not "none".
    data: list[str] = Field(default_factory=list)


class Transport(Strict):
    """How to reach the server. ``stdio`` launches it; ``http`` connects to it."""

    kind: Literal["stdio", "http"]
    #: argv for ``kind: stdio``. It is launched in the project directory — the
    #: one holding this file's ``.itest/`` — so a relative path in it resolves
    #: there, never against the caller's working directory or a repository root.
    #: A bare ``python`` / ``python3`` first word is the interpreter ITest runs
    #: under (see ``tools.build_target``).
    command: list[str] | None = None
    #: The NAME of the environment variable holding the base URL for
    #: ``kind: http``. Present alongside a stdio command when the same server
    #: can also be probed over HTTP.
    url_env: str | None = None

    @field_validator("url_env")
    @classmethod
    def _check_url_env(cls, value: str | None) -> str | None:
        return _env_name(value, "transport.url_env")

    @model_validator(mode="after")
    def _check_kind(self) -> Transport:
        if self.kind == "stdio" and not self.command:
            raise ValueError(
                "transport.kind 'stdio' needs a command: the argv that launches "
                "the server, e.g. [python, path/to/server.py]."
            )
        if self.kind == "http" and not self.url_env:
            raise ValueError(
                "transport.kind 'http' needs a url_env: the NAME of the "
                "environment variable holding the base URL."
            )
        return self


class Auth(Strict):
    """How the server authenticates a caller. Names only, never values."""

    scheme: Literal["none", "bearer"] = "none"
    #: The NAME of the env var holding the primary credential.
    credential_env: str | None = None
    #: The NAME of the env var holding a *second tenant's* credential. Its
    #: presence is what makes a tenant-isolation check possible at all: with one
    #: identity there is nothing to cross.
    second_tenant_env: str | None = None
    #: Whether a ``stdio`` server checks a credential of its own rather than
    #: trusting whoever spawned it. Over stdio the process boundary *is* the
    #: authentication boundary — there is no anonymous caller — so the
    #: anonymous-refusal check applies to a stdio server only when its owner
    #: states this. A fact only the server's owner knows; ``false`` withholds
    #: the check, it never loosens anything.
    enforced_over_stdio: bool = False

    @field_validator("credential_env")
    @classmethod
    def _check_credential_env(cls, value: str | None) -> str | None:
        return _env_name(value, "auth.credential_env")

    @field_validator("second_tenant_env")
    @classmethod
    def _check_second_tenant_env(cls, value: str | None) -> str | None:
        return _env_name(value, "auth.second_tenant_env")

    @model_validator(mode="after")
    def _check_scheme(self) -> Auth:
        if self.scheme == "bearer" and not self.credential_env:
            raise ValueError(
                "auth.scheme 'bearer' needs a credential_env naming the "
                "environment variable that holds the token."
            )
        if self.scheme == "none" and self.credential_env:
            raise ValueError(
                "auth.scheme 'none' cannot carry a credential_env: one of the "
                "two is wrong, and nothing here can know which."
            )
        if self.second_tenant_env and not self.credential_env:
            raise ValueError(
                "auth.second_tenant_env names a second tenant but there is no "
                "credential_env for the first. A second identity is only "
                "meaningful beside a first one."
            )
        return self


class Tenancy(Strict):
    """What decides which tenant's data a call can reach.

    Omitted means ``credential`` — the shape a tenant-isolation check is written
    against. Say ``parameter`` or ``both`` when a tenant id rides in the
    arguments, because then the check has to vary the argument too.
    """

    scoped_by: Literal["credential", "parameter", "both"] = "credential"


class Identity(Strict):
    """Whose authority the server acts with."""

    runs_as: Literal["passthrough", "service", "per_tenant"] = "passthrough"
    #: The role, user or service account behind ``service`` / ``per_tenant``.
    backing_role: str | None = None


class Audit(Strict):
    """Where a mutation is recorded.

    ``sink`` absent means there is nowhere to look, so no audit check is
    generated — rather than one generated and quietly asserting nothing.
    """

    sink: str | None = None


class Approval(Strict):
    """What has to happen before a destructive call goes through."""

    destructive_requires: ApprovalRule = "none"


class Sentinels(Strict):
    """Arguments that make a mutating probe safe to run.

    ``nonexistent_id`` is required because it is what lets a probe prove a guard
    by being **admitted** rather than by destroying something.
    """

    nonexistent_id: str = Field(min_length=1)
    #: A record that exists and is safe to read, for a happy-path read.
    readable_record: str | None = None


class Environments(Strict):
    """Where the active (mutating) tier may run for this server.

    Every name here must already be permitted ``active`` by the committed
    ``.itest/environments.yaml``: a declaration can narrow the policy, never
    widen it. The loader enforces that, because the policy is the code-reviewed
    artifact and this file is not.
    """

    active_allowed_in: list[str] = Field(default_factory=list)


class Defaults(Strict):
    """What every tool on this server is taken to be, before its own override."""

    mutation: DeclaredMutation = "detect"
    egress: Egress | Literal["none"] = "none"
    #: Whether active-tier checks may be generated for this server's tools at
    #: all. ``false`` withholds them; it never loosens anything.
    active: bool = True


class ToolOverride(Strict):
    """What one named tool says about itself, where it differs from the defaults.

    Every field is optional, and ``None`` means *silent* — inherit the default.
    That is deliberately different from a stated value that happens to match:
    ``egress: none`` on a tool is a statement, and silence is not.
    """

    mutation: DeclaredMutation | None = None
    egress: Egress | Literal["none"] | None = None
    approval: ApprovalRule | None = None
    active: bool | None = None
    notes: str | None = None
    #: The traits this tool gets, when the applies-when table must be overridden
    #: by hand. ``[none-of-these]`` says it needs no checks and requires notes.
    traits: list[str] | None = None

    @field_validator("traits")
    @classmethod
    def _check_traits(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError(
                "traits is an empty list. Omit the key to inherit the table, or "
                f"write [{NO_TRAITS}] with notes to say the tool needs none."
            )
        value = [migrate_trait_id(entry) for entry in value]
        if NO_TRAITS in value and len(value) > 1:
            raise ValueError(
                f"traits '{NO_TRAITS}' cannot sit beside a trait id: either the "
                "tool needs no checks or it needs those ones."
            )
        for entry in value:
            if entry != NO_TRAITS and not _TRAIT_ID.match(entry):
                raise ValueError(
                    f"traits entry {entry!r} is not a trait id (a family and a "
                    f"slug, e.g. authority.anonymous) or '{NO_TRAITS}'."
                )
        repeated = sorted({entry for entry in value if value.count(entry) > 1})
        if repeated:
            raise ValueError(
                f"traits lists {', '.join(repeated)} more than once. Each trait "
                "is one check; list it once."
            )
        return value

    @model_validator(mode="after")
    def _check_notes(self) -> ToolOverride:
        if self.traits and NO_TRAITS in self.traits and not self.notes:
            raise ValueError(
                f"traits '{NO_TRAITS}' requires notes. Declaring that a tool "
                "needs no checks at all is a person's claim, and it has to be "
                "signed in writing where a reviewer will read it."
            )
        return self


class Declaration(Strict):
    """One MCP server, as declared in ``.itest/tools/<server>.yaml``."""

    server: str
    transport: Transport
    auth: Auth = Field(default_factory=Auth)
    tenancy: Tenancy = Field(default_factory=Tenancy)
    identity: Identity = Field(default_factory=Identity)
    audit: Audit = Field(default_factory=Audit)
    approval: Approval = Field(default_factory=Approval)
    sentinels: Sentinels
    environments: Environments = Field(default_factory=Environments)
    defaults: Defaults = Field(default_factory=Defaults)
    tools: dict[str, ToolOverride] = Field(default_factory=dict)

    @field_validator("server")
    @classmethod
    def _check_server(cls, value: str) -> str:
        if not _SERVER_NAME.match(value):
            raise ValueError(
                f"server {value!r} must be lower-case letters, digits and "
                "hyphens: it is the file name, half of every point id, and a "
                "fragment of every generated test name."
            )
        return value
