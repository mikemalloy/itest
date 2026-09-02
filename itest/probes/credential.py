"""Resolve an active-tier test credential from a *named* environment variable.

The token itself never enters the tool. The skill records only the NAME of an
env var; the value lives in the shell environment or a gitignored
``.itest/.env`` the user creates. This module reads that value and hands it to a
probe as a header — it never stores, logs, or echoes it.

Design rules, each load-bearing:

- **Only a name crosses the boundary.** :func:`resolve_credential` takes the env
  var's name and returns its value or ``None``. It has no parameter for, and no
  memory of, the secret.
- **The shell always wins.** ``.itest/.env`` seeds only keys not already set in
  ``os.environ`` (the dotenv convention), so a value exported in the shell is
  never overridden by the file.
- **Absent is not an error.** A missing name, an unset var, or an empty value all
  resolve to ``None`` — authenticated probes are simply not generated. No
  placeholder, no exception.
- **A failure names the file, never the value.** The only error this module
  raises is an *unreadable* ``.itest/.env`` (it exists but cannot be read). Its
  message carries the path, never the token that may be sitting in the
  environment — a leak in a traceback is exactly what this module exists to
  prevent.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE_RELATIVE = Path(".itest") / ".env"


class CredentialError(Exception):
    """An unreadable ``.itest/.env``. Names the file path, never any value."""


def _parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Blank lines, ``#`` comments, and lines with no
    ``=`` (or an empty key) are skipped; surrounding single/double quotes are
    stripped from the value. Deliberately tiny — no python-dotenv dependency."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        parsed[key] = value
    return parsed


def resolve_credential(env_name: str, base_dir: Path | None = None) -> str | None:
    """Return the value of ``env_name``, or ``None`` if unset or empty.

    When ``base_dir`` is given and ``<base_dir>/.itest/.env`` exists, its
    ``KEY=VALUE`` lines seed ``os.environ`` for keys not already present (the
    shell wins). The token is never returned in, or attached to, any raised
    error: the sole error, :class:`CredentialError`, means the ``.env`` file
    could not be read and names only its path.
    """
    if base_dir is not None:
        env_file = Path(base_dir) / ENV_FILE_RELATIVE
        if env_file.exists():
            try:
                text = env_file.read_text(encoding="utf-8")
            except OSError:
                # Name the file, never the token that may be in os.environ.
                raise CredentialError(f"could not read {env_file}") from None
            for key, value in _parse_env_file(text).items():
                os.environ.setdefault(key, value)

    if not env_name:
        return None
    return os.environ.get(env_name) or None
