"""The ``itest redact`` engine.

Takes a ``terraform show -json`` plan or state document and returns a copy that
is safe to share, commit as a fixture, or paste into a conversation.

Four passes, all on by default:

1. **Terraform's own markings.** Plan and state documents carry a parallel
   ``sensitive_values`` / ``after_sensitive`` structure. Terraform already knows
   what is secret; every marking is honoured rather than second-guessed.
2. **Lambda environment variables**, allowlist-only. Keys are kept so the shape
   of the config stays readable; values go unless the key is provably dull.
   Guessing which env vars are safe is how secrets leak, so the default is to
   drop the value.
3. **Pattern scrubbing** over every remaining string, for credentials that are
   in the document because someone put them somewhere Terraform does not mark.
4. **Account-ID pseudonymization**, stable within a document, so ARNs still
   correlate with one another after redaction.

Structure, addresses, resource names, and non-secret values are left alone: the
output must remain a document ITest's own detectors accept.
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

PLACEHOLDER = "REDACTED"

# Terraform's parallel sensitivity structures: value block -> its mask.
_SENSITIVE_SIBLINGS = {
    "values": "sensitive_values",
    "before": "before_sensitive",
    "after": "after_sensitive",
}

_LAMBDA_TYPE = "aws_lambda_function"

# Allowlist-only: a Lambda env value survives only if its key is one of these.
_ENV_ALLOWED_EXACT = frozenset({"DATABASE_NAME"})
_ENV_ALLOWED_SUFFIXES = ("_REGION", "_ARN")

_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # OpenAI and friends: sk-… / sk-proj-…
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    # Secret access key ids. Only AKIA/ASIA belong here: the other AWS unique-id
    # prefixes name principals rather than authenticate as them, so they are
    # pseudonymized as identifiers below instead of being blanked.
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
)

# `scheme://user:password@host` — keep the shape, drop the password.
_CONNECTION_STRING = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://[^:/@\s]+:)([^@/\s]+)(@)")

# `Bearer <token>` / `Authorization: token <token>` — keep the scheme word.
_BEARER = re.compile(r"\b([Bb]earer\s+)([A-Za-z0-9._~+/=-]{10,})")

# A single opaque token, long enough and mixed enough to be a credential.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9+/=_-]+")
_MIN_TOKEN_LENGTH = 32
# Pure-hex digests top out near 4.0 bits/char; mixed-alphabet secrets run well
# above it. Sitting above hex spares checksums and resource ids, which are not
# secrets and whose loss would corrupt the document for no benefit.
_MIN_TOKEN_ENTROPY = 4.2

_ACCOUNT_ID = re.compile(r"\b\d{12}\b")

# Identifiers, not credentials: they authenticate nobody, but they fingerprint
# an account, so they are pseudonymized rather than blanked. Keeping the prefix
# and the length means the document still reads as IAM or RDS afterwards.
# Each entry is (name, pattern, prefix length).
_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "iam_principal_id",
        re.compile(r"\b(?:AIDA|AROA|AGPA|AIPA|ANPA|ANVA|ABIA|ACCA)[0-9A-Z]{16,}\b"),
        4,
    ),
    ("db_resource_id", re.compile(r"\bdb-[A-Z0-9]{10,}\b"), 3),
)

#: Keys whose value is an account-fingerprinting identifier whatever its shape.
#: A safety net for forms the patterns above do not anticipate.
_IDENTIFIER_KEYS = frozenset({"unique_id", "dbi_resource_id"})

#: Marks a value this module already pseudonymized, which keeps redaction
#: idempotent the same way a repeated-digit account id does.
_PSEUDONYM_TAG = "EXAMPLE"


class Finding(BaseModel):
    """One redaction that was applied, or would be by ``--check``.

    Deliberately carries no secret material: a findings report is written to
    stdout and pasted into CI logs, so it must be safe on its own.
    """

    path: str
    category: str
    detail: str = ""


class RedactionResult(BaseModel):
    """Machine-readable summary of a redaction pass."""

    finding_count: int = 0
    findings: list[Finding] = Field(default_factory=list)


def _shannon_entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum(n / length * math.log2(n / length) for n in counts.values())


def _looks_like_opaque_token(value: str) -> bool:
    """True for a lone high-entropy blob, the shape of an API token."""
    if len(value) < _MIN_TOKEN_LENGTH or not _TOKEN_SHAPE.fullmatch(value):
        return False
    return _shannon_entropy(value) >= _MIN_TOKEN_ENTROPY


def _env_key_allowed(key: str) -> bool:
    upper = key.upper()
    return upper in _ENV_ALLOWED_EXACT or upper.endswith(_ENV_ALLOWED_SUFFIXES)


def _is_pseudonym(account_id: str) -> bool:
    """True for an id this module already produced, e.g. ``111111111111``."""
    return len(set(account_id)) == 1


class _Accounts:
    """Stable real-account -> fake-account mapping for one document.

    Assignment follows first-encounter order, and an id that is already a
    pseudonym maps to itself, which together make redaction idempotent.
    """

    def __init__(self) -> None:
        self._mapping: dict[str, str] = {}
        self._used: set[str] = set()
        self._next = 1

    def pseudonym_for(self, account_id: str) -> str:
        if account_id in self._mapping:
            return self._mapping[account_id]

        if _is_pseudonym(account_id):
            fake = account_id
        else:
            while True:
                fake = str(self._next) * 12 if self._next <= 9 else f"{self._next:012d}"
                self._next += 1
                if fake not in self._used:
                    break

        self._mapping[account_id] = fake
        self._used.add(fake)
        return fake


class _Identifiers:
    """Stable real-identifier -> fake-identifier mapping for one document.

    Mirrors _Accounts: first-encounter order, and a value already carrying the
    pseudonym tag maps to itself, so repeated occurrences still correlate and
    redacting twice changes nothing.
    """

    def __init__(self) -> None:
        self._mapping: dict[str, str] = {}
        self._next = 1

    def pseudonym_for(self, value: str, prefix_length: int) -> str:
        if value in self._mapping:
            return self._mapping[value]

        prefix, body = value[:prefix_length], value[prefix_length:]
        if body.startswith(_PSEUDONYM_TAG):
            fake = value
        else:
            tag = f"{_PSEUDONYM_TAG}{self._next}"
            self._next += 1
            # Preserve length so the value still looks like what it is.
            body = (
                tag[: len(body)]
                if len(tag) >= len(body)
                else tag + "0" * (len(body) - len(tag))
            )
            fake = prefix + body

        self._mapping[value] = fake
        return fake


class _Redactor:
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self._accounts = _Accounts()
        self._identifiers = _Identifiers()

    def _record(self, path: str, category: str, detail: str) -> None:
        self.findings.append(Finding(path=path, category=category, detail=detail))

    # -- string-level passes -------------------------------------------------

    def _scrub_string(self, value: str, path: str) -> str:
        """Apply credential patterns, then pseudonymize account ids."""
        if value == PLACEHOLDER:
            return value

        if _looks_like_opaque_token(value):
            self._record(
                path,
                "credential_pattern",
                "high-entropy token (length "
                f"{len(value)}, entropy {_shannon_entropy(value):.1f})",
            )
            return PLACEHOLDER

        scrubbed = value
        # A finding means something actually changed, not merely that a pattern
        # matched: an already-scrubbed value like `postgres://user:REDACTED@host`
        # still matches, and counting that would break idempotence.
        replacements: tuple[tuple[str, re.Pattern[str], str], ...] = (
            *((name, pattern, PLACEHOLDER) for name, pattern in _CREDENTIAL_PATTERNS),
            ("bearer_token", _BEARER, rf"\1{PLACEHOLDER}"),
            ("connection_string", _CONNECTION_STRING, rf"\1{PLACEHOLDER}\3"),
        )
        for name, pattern, replacement in replacements:
            replaced, count = pattern.subn(replacement, scrubbed)
            if replaced != scrubbed:
                self._record(path, "credential_pattern", f"{name} x{count}")
                scrubbed = replaced

        scrubbed = self._pseudonymize_identifiers(scrubbed, path)
        return self._pseudonymize_accounts(scrubbed, path)

    def _pseudonymize_identifiers(self, value: str, path: str) -> str:
        result = value
        for name, pattern, prefix_length in _IDENTIFIER_PATTERNS:
            replaced = pattern.sub(
                lambda m, n=prefix_length: self._identifiers.pseudonym_for(
                    m.group(0), n
                ),
                result,
            )
            if replaced != result:
                self._record(path, "identifier", f"{name} pseudonymized")
                result = replaced

        # Safety net for a known identifier key whose value the patterns above
        # do not recognise; the whole value is replaced, length preserved.
        key = path.rsplit(".", 1)[-1]
        if key in _IDENTIFIER_KEYS and result == value and result.strip():
            replaced = self._identifiers.pseudonym_for(result, 0)
            if replaced != result:
                self._record(path, "identifier", f"{key} pseudonymized")
                result = replaced

        return result

    def _pseudonymize_accounts(self, value: str, path: str) -> str:
        changed = False

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            original = match.group(0)
            fake = self._accounts.pseudonym_for(original)
            if fake != original:
                changed = True
            return fake

        result = _ACCOUNT_ID.sub(replace, value)
        if changed:
            self._record(path, "account_id", "12-digit account id pseudonymized")
        return result

    # -- structural passes ---------------------------------------------------

    def _apply_sensitivity(self, value: Any, mask: Any, path: str) -> Any:
        """Blank out everything Terraform flagged in the parallel mask."""
        if mask is True:
            if value != PLACEHOLDER:
                self._record(path, "sensitive_value", "marked sensitive by Terraform")
            return PLACEHOLDER
        if isinstance(mask, dict) and isinstance(value, dict):
            return {
                key: self._apply_sensitivity(item, mask.get(key), f"{path}.{key}")
                for key, item in value.items()
            }
        if isinstance(mask, list) and isinstance(value, list):
            return [
                self._apply_sensitivity(
                    item, mask[i] if i < len(mask) else None, f"{path}[{i}]"
                )
                for i, item in enumerate(value)
            ]
        return value

    def _redact_lambda_env(self, variables: dict, path: str) -> dict:
        result = {}
        for key, value in variables.items():
            if _env_key_allowed(key):
                result[key] = (
                    self._scrub_string(value, f"{path}.{key}")
                    if isinstance(value, str)
                    else value
                )
                continue
            if value != PLACEHOLDER:
                self._record(
                    path + f".{key}",
                    "lambda_env",
                    "lambda environment value (key not on the allowlist)",
                )
            result[key] = PLACEHOLDER
        return result

    def _walk_lambda(self, resource: dict, path: str) -> None:
        """Redact env vars in place on an already-copied Lambda resource."""
        environment = resource.get("values", {}).get("environment")
        blocks = environment if isinstance(environment, list) else [environment]
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            variables = block.get("variables")
            if isinstance(variables, dict):
                block["variables"] = self._redact_lambda_env(
                    variables, f"{path}.values.environment[{i}].variables"
                )

    def _walk(self, node: Any, path: str) -> Any:
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                child = f"{path}.{key}" if path else key
                mask_key = _SENSITIVE_SIBLINGS.get(key)
                if mask_key and mask_key in node:
                    value = self._apply_sensitivity(value, node[mask_key], child)
                result[key] = self._walk(value, child)

            if result.get("type") == _LAMBDA_TYPE:
                self._walk_lambda(result, path)
            return result

        if isinstance(node, list):
            return [self._walk(item, f"{path}[{i}]") for i, item in enumerate(node)]

        if isinstance(node, str):
            return self._scrub_string(node, path)

        return node


def account_pseudonymizer() -> Callable[[str], str]:
    """Return a stateful text rewriter for AWS account ids.

    Each distinct account maps to a stable fake (111111111111, 222222222222,
    …) for the life of the returned function, so repeated occurrences still
    correlate. Shared with ``itest verify --redact`` so both commands scrub
    account ids the same way from one pattern and one mapping.
    """
    accounts = _Accounts()

    def replace(text: str) -> str:
        return _ACCOUNT_ID.sub(lambda m: accounts.pseudonym_for(m.group(0)), text)

    return replace


def redact_document(document: dict) -> tuple[dict, list[Finding]]:
    """Return a sanitized copy of ``document`` plus what was redacted.

    The input is never mutated. Redaction is idempotent: running it over its
    own output yields an identical document and no findings.
    """
    redactor = _Redactor()
    clean = redactor._walk(copy.deepcopy(document), "")
    return clean, redactor.findings


def render_findings(findings: list[Finding]) -> str:
    """Human summary for ``--check``. Never includes secret material."""
    if not findings:
        return "No secrets found. Safe to share."

    by_category: dict[str, list[Finding]] = {}
    for finding in findings:
        by_category.setdefault(finding.category, []).append(finding)

    out = [f"{len(findings)} finding(s) — this document is NOT safe to share."]
    for category in sorted(by_category):
        group = by_category[category]
        out.append("")
        out.append(f"{category} ({len(group)}):")
        for finding in group:
            out.append(f"  {finding.path}")
            if finding.detail:
                out.append(f"      {finding.detail}")
    out.append("")
    out.append("Run without --check to write a sanitized copy.")
    return "\n".join(out)
