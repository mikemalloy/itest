"""Evidence sources: a red-team tool's results, read from a file and joined
onto the tools ITest already inventories.

ITest asserts boundaries and produces facts. An evidence source adds a second,
different *kind* of evidence to the same page: how often an agent could be
talked into calling a tool, as a dated rate beside the boundary checks. Red
says how often an agent could be induced; blue says whether the tool would let
it. The pair sits on one row; neither replaces the other.

The rule that governs every module here: **judgment evidence is not a check.**
It has no lifecycle state, never enters ``lifecycle.COUNTED_STATES``, never
changes ``ToolLedger.verified()``, never changes the verdict, never appears in
coverage, and its standards ids never enter the standards rollup's covered
count. A passing red-team run can never turn a cell green.

ITest never runs the tool, never shells out, never calls a model: the reader is
file I/O and arithmetic. No credential is named or read — a results path is a
path.

- :mod:`itest.core.evidence.schema` — what a source may state.
- :mod:`itest.core.evidence.loader` — reading ``.itest/sources/*.yaml``.
"""

from __future__ import annotations

from itest.core.evidence.loader import (
    SOURCES_DIR,
    LoadedSource,
    SourceProblem,
    SourcesLoad,
    load_sources,
    source_path,
    sources_dir,
)
from itest.core.evidence.schema import DEFAULT_MAX_AGE_DAYS, EvidenceSource

__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "SOURCES_DIR",
    "EvidenceSource",
    "LoadedSource",
    "SourceProblem",
    "SourcesLoad",
    "load_sources",
    "source_path",
    "sources_dir",
]
