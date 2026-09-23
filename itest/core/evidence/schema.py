"""The evidence-source schema: what may be stated in ``.itest/sources/<name>.yaml``.

A source is a pointer to a red-team tool's results file and the declared server
those results are about. It states **facts about a file**, never what the file
proves:

- ``kind`` names the tool whose output format the reader understands.
  ``promptfoo`` is the only kind this build accepts; a later kind (garak, say)
  is the same shape with a different reader.
- ``server`` is a server declared under ``.itest/tools/``. The join is on
  ``(server, tool name)`` and nothing else.
- exactly one of ``results`` (a path, relative to the project directory) and
  ``results_env`` (the NAME of an environment variable holding the path, for
  CI). A results path is not a secret and may be written literally; the
  env-var form exists so a pipeline can point at wherever it put the file.
- ``standards`` are the published ids this evidence bears on, validated with
  the same rule the trait table's ``standards`` column uses. They are shown as
  *external evidence*, never as coverage.
- ``max_age_days`` is how old a run may be before it is shown as stale
  (shown, and — like everything here — uncounted).

**Unknown keys are errors**, as in a declaration: a typo would otherwise be a
fact silently not stated.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from itest.core.declarations.traits import STANDARDS_PREFIXES, is_standard_id

#: The tools whose results this build can read. One reader per kind.
EvidenceKind = Literal["promptfoo"]

#: How old a run may be, in days from the sync that reads it, before it is
#: shown as stale. Seven: a red-team run is a statement about a model and a
#: prompt set on a day, not a property of the server.
DEFAULT_MAX_AGE_DAYS = 7

#: A server name, as the declaration schema spells it.
_SERVER_NAME = re.compile(r"^[a-z0-9-]+$")

#: An environment variable name. Anything with a ``/``, a ``:`` or a space is
#: a path pasted where a name goes.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Strict(BaseModel):
    """Base for every evidence model: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


class EvidenceSource(Strict):
    """One declared source: a results file and the server it is about."""

    kind: EvidenceKind
    server: str
    #: A results file, relative to the project directory (the one holding
    #: ``.itest/``). Not a secret.
    results: str | None = None
    #: The NAME of an environment variable holding the results path.
    results_env: str | None = None
    #: Free text naming the agent the tool drove (``sonnet-5 via agent.js``).
    #: Shown on the source line; never interpreted.
    agent: str | None = None
    #: The published ids this evidence bears on. Shown as external evidence.
    standards: list[str] = Field(default_factory=list)
    max_age_days: int = Field(default=DEFAULT_MAX_AGE_DAYS, ge=0)

    @field_validator("server")
    @classmethod
    def _check_server(cls, value: str) -> str:
        if not _SERVER_NAME.match(value):
            raise ValueError(
                f"server {value!r} must be lower-case letters, digits and "
                "hyphens: it names a declaration under .itest/tools/."
            )
        return value

    @field_validator("results_env")
    @classmethod
    def _check_results_env(cls, value: str | None) -> str | None:
        if value is not None and not _ENV_NAME.match(value):
            raise ValueError(
                "results_env must be the NAME of an environment variable "
                "(letters, digits and underscores). To name the file itself, "
                "write it under `results` instead."
            )
        return value

    @field_validator("standards")
    @classmethod
    def _check_standards(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not is_standard_id(entry):
                raise ValueError(
                    f"standards entry {entry!r} is not a published id this build "
                    f"recognises. Known prefixes: {'; '.join(STANDARDS_PREFIXES)}. "
                    "Never invent one."
                )
        return value

    @model_validator(mode="after")
    def _check_results(self) -> EvidenceSource:
        if (self.results is None) == (self.results_env is None):
            raise ValueError(
                "exactly one of `results` (a path relative to the project "
                "directory) and `results_env` (the NAME of an environment "
                "variable holding that path) must be given."
            )
        return self
