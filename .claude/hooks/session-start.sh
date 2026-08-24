#!/bin/bash
# SessionStart hook — prepares a Claude Code on the web container to run the
# ITest test suite and CLI.
#
# Mirrors the manual setup in README.md / HANDOFF.md:
#   python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
#
# Local sessions are left alone: developers there manage their own virtualenv.
set -euo pipefail

# Remote (web) sessions only.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"

VENV=".venv"

# Idempotent: reuse an existing venv, and rebuild one that is broken or was
# created against a Python that is no longer present in the image.
if [ ! -x "$VENV/bin/python" ]; then
  rm -rf "$VENV"
  python3 -m venv "$VENV"
fi

# Editable install so `itest` on PATH tracks the working tree, plus pytest.
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e ".[dev]"

# Put the venv first on PATH for the rest of the session, so `pytest` and
# `itest` work without an explicit `source .venv/bin/activate`.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$(pwd)/$VENV/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
  echo "export VIRTUAL_ENV=\"$(pwd)/$VENV\"" >> "$CLAUDE_ENV_FILE"
fi

echo "ITest environment ready: $("$VENV/bin/python" --version), $("$VENV/bin/pytest" --version 2>&1 | head -1)"
