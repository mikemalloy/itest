"""Why a check was not run here: one closed vocabulary of reason codes.

A check that did not run says why with a code from :data:`REASONS`, and
nothing downstream reads its detail string to find out. The check library
names a code on every ``not_verifiable`` result (the keyword is required);
verify stamps one on every outcome it decides itself — a check held out by
the environment policy, a skipped or missing test, a check with no test
registered, a point still at stub. The readiness page's Answer maps each
code to one plain sentence (``itest.report.model.REASON_SENTENCES``) and
refuses a code it does not know rather than printing raw text; that is what
keeps an engine string off the page.

The verifier and the check library both import this module; the report
imports it too. Nothing here imports anything.
"""

from __future__ import annotations

# --- held out by the environment policy -------------------------------------------

#: A tier the policy withholds, and nothing is bound: no policy at all, or a
#: policy with no binding (the safe floor).
HELD_OUT_UNBOUND = "held_out.unbound"
#: Bound to a production environment, which never runs the active tier.
HELD_OUT_PRODUCTION = "held_out.production"
#: Bound to a non-production environment whose policy leaves the tier out.
HELD_OUT_WITHHELD = "held_out.withheld"

# --- the check could not be attempted ---------------------------------------------

#: A mutating tool behind an open front door: only an active-tier call can
#: prove its guard, and that check is not written yet.
DEFERRED = "deferred"
#: The declaration lacks a fact the check needs (a snapshot tool, a sentinel).
UNDECLARED = "undeclared"
#: A generated check whose conftest fixture is still the placeholder.
NEEDS_FACTS = "needs_facts"
#: No engine or generated check exists for the trait yet.
UNWRITTEN = "unwritten"
#: No test is registered for the check (``itest sync`` has not run).
UNREGISTERED = "unregistered"
#: A point whose test is still the generated stub.
STUB = "stub"
#: A point whose manifest entry says its test is implemented, yet the run
#: reported stub: the test verified nothing. Pending a reviewer, not a stub.
STUCK = "stuck"
#: authority.anonymous on a stdio server that checks no credential of its
#: own: the process boundary is the authentication boundary.
STDIO_BOUNDARY = "stdio_boundary"
#: The observed mutation check on a tool that already says it mutates.
ALREADY_MUTATING = "already_mutating"
#: The tool's class could not be told (unknown, or only the declaration says).
UNCLASSIFIED = "unclassified"
#: No safe way to call the tool could be set up: a parameter with no sentinel
#: form, a snapshot tool that is not a read tool, not listed, or unstable.
NO_SAFE_CALL = "no_safe_call"
#: The server no longer lists the tool.
NOT_LISTED = "not_listed"
#: The server, session, listing or call could not be completed.
UNREACHABLE = "unreachable"
#: A credential was needed and none resolved.
NO_CREDENTIAL = "no_credential"
#: pytest skipped the test for a reason of its own (not a placeholder).
SKIPPED = "skipped"
#: pytest reported no result for a registered test.
MISSING = "missing"

#: Every code, in the order the Answer lists them.
REASONS = (
    HELD_OUT_UNBOUND,
    HELD_OUT_PRODUCTION,
    HELD_OUT_WITHHELD,
    DEFERRED,
    UNDECLARED,
    NEEDS_FACTS,
    UNWRITTEN,
    UNREGISTERED,
    STUB,
    STUCK,
    STDIO_BOUNDARY,
    ALREADY_MUTATING,
    UNCLASSIFIED,
    NO_SAFE_CALL,
    NOT_LISTED,
    UNREACHABLE,
    NO_CREDENTIAL,
    SKIPPED,
    MISSING,
)

#: The phrase the generated conftest's placeholder fixture skips with. It is
#: what tells a "fill in your conftest" skip from any other skip; stubgen
#: writes it and the verifier reads it.
NEEDS_FACTS_MARKER = "needs facts only you can supply"


def check(code: str) -> str:
    """``code``, or a ``ValueError`` naming it when it is not in the vocabulary."""
    if code not in REASONS:
        raise ValueError(
            f"{code!r} is not a not-run reason code; the vocabulary is "
            f"{', '.join(REASONS)}."
        )
    return code


def held_out_reason(resolution) -> str:
    """Why this run holds a tier out, from the environment resolution."""
    if resolution.environment is None:
        return HELD_OUT_UNBOUND
    if resolution.production:
        return HELD_OUT_PRODUCTION
    return HELD_OUT_WITHHELD


def skip_reason(text: str) -> str:
    """The code for a pytest skip: a placeholder fixture, or a plain skip."""
    return NEEDS_FACTS if NEEDS_FACTS_MARKER in (text or "") else SKIPPED
