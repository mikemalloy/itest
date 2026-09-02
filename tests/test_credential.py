"""The credential resolver: a named env var, optionally seeded from .itest/.env.

The tool never stores the token. `resolve_credential` reads the *name* of an
env var and returns its value from the process environment, seeding unset keys
from a gitignored `.itest/.env` (the shell always wins). A missing name or unset
var is None, never an error — absent credential means unchanged behavior. And
the value must never leak into an error's text.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from itest.probes.credential import CredentialError, resolve_credential

NAME = "ITEST_API_TOKEN"


@pytest.fixture(autouse=True)
def _isolated_env():
    """Snapshot and restore os.environ, since resolve_credential seeds it."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _write_env(base_dir: Path, body: str) -> None:
    itest = base_dir / ".itest"
    itest.mkdir(parents=True, exist_ok=True)
    (itest / ".env").write_text(body, encoding="utf-8")


def test_value_read_from_env_file(tmp_path: Path) -> None:
    os.environ.pop(NAME, None)
    _write_env(tmp_path, f"{NAME}=file-token\n")
    assert resolve_credential(NAME, tmp_path) == "file-token"


def test_shell_env_overrides_the_file(tmp_path: Path) -> None:
    os.environ[NAME] = "shell-token"
    _write_env(tmp_path, f"{NAME}=file-token\n")
    assert resolve_credential(NAME, tmp_path) == "shell-token"


def test_missing_name_is_none(tmp_path: Path) -> None:
    _write_env(tmp_path, "OTHER=x\n")
    assert resolve_credential("NOT_CONFIGURED", tmp_path) is None


def test_empty_value_is_none(tmp_path: Path) -> None:
    os.environ.pop(NAME, None)
    _write_env(tmp_path, f"{NAME}=\n")
    assert resolve_credential(NAME, tmp_path) is None


def test_no_base_dir_reads_only_the_environment(tmp_path: Path) -> None:
    os.environ[NAME] = "shell-only"
    # A .env exists but base_dir is None, so it is not consulted.
    _write_env(tmp_path, f"{NAME}=file-token\n")
    assert resolve_credential(NAME, None) == "shell-only"


def test_quoted_values_are_unquoted(tmp_path: Path) -> None:
    os.environ.pop("DQ", None)
    os.environ.pop("SQ", None)
    _write_env(tmp_path, "DQ=\"double-quoted\"\nSQ='single-quoted'\n")
    assert resolve_credential("DQ", tmp_path) == "double-quoted"
    assert resolve_credential("SQ", tmp_path) == "single-quoted"


def test_malformed_lines_comments_and_blanks_are_skipped(tmp_path: Path) -> None:
    os.environ.pop(NAME, None)
    _write_env(
        tmp_path,
        "# a comment\n"
        "\n"
        "no_equals_sign_here\n"
        "=value-with-no-key\n"
        f"  {NAME} = good-token  \n",
    )
    assert resolve_credential(NAME, tmp_path) == "good-token"


def test_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    os.environ.pop(NAME, None)
    # No .itest/.env at all: a missing credential is None, not a raise.
    assert resolve_credential(NAME, tmp_path) is None


def test_error_never_leaks_the_token_value(tmp_path: Path) -> None:
    """When the resolver does raise (an unreadable .itest/.env), the error must
    name neither the token that is set in the environment nor any file content."""
    secret = "s3cr3t-token-value-xyz"
    os.environ[NAME] = secret
    itest = tmp_path / ".itest"
    itest.mkdir()
    # A directory where a file is expected: read_text raises, resolver re-raises.
    (itest / ".env").mkdir()

    with pytest.raises(CredentialError) as excinfo:
        resolve_credential(NAME, tmp_path)

    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)
