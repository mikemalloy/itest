"""Load, validate and resolve the evidence sources under ``.itest/sources/``.

One file per source, named for it: ``.itest/sources/<name>.yaml``. The loader
turns those files into validated :class:`~.schema.EvidenceSource` objects with
their results path resolved, and reports — never raises — what it could not:

- a file that is not valid YAML, not a mapping, or not a valid source is a
  :class:`SourceProblem` in ``errors``, beside the sources that did load. One
  bad source never hides the others.
- a source whose results file cannot be found (the variable unset, the path
  absent) still loads, with ``problem`` set. Sync records it as ``unreadable``
  and carries on: evidence is best-effort, never blocking.

Resolution: ``results_env`` is read from the environment; ``results`` is taken
as written. A relative path is relative to the project directory (the one that
holds ``.itest/``), never to the caller's working directory. A results path is
not a credential, so it is named plainly in every message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from itest.core.evidence.schema import EvidenceSource
from itest.core.planner import ITEST_DIR

#: Where sources live, relative to the project root.
SOURCES_DIR = "sources"


def sources_dir(base_dir: Path) -> Path:
    return base_dir / ITEST_DIR / SOURCES_DIR


def source_path(name: str) -> str:
    """The repo-relative path a source's file lives at."""
    return f"{ITEST_DIR}/{SOURCES_DIR}/{name}.yaml"


@dataclass(frozen=True)
class SourceProblem:
    """A source file that could not be loaded: its name, path and why."""

    name: str
    path: str
    message: str


@dataclass(frozen=True)
class LoadedSource:
    """A validated source and where its results file resolved to.

    ``results_path`` is ``None`` when ``results_env`` is unset; ``problem`` is
    set whenever the file cannot be read at this point (unset variable, path
    absent) and says why. Neither is an exception.
    """

    name: str
    source: EvidenceSource
    results_path: Path | None
    problem: str | None = None


@dataclass(frozen=True)
class SourcesLoad:
    """Everything under ``.itest/sources/``: what loaded, and what did not."""

    sources: list[LoadedSource]
    errors: list[SourceProblem]


def _format_validation_error(path: str, exc: ValidationError) -> str:
    lines = [f"{path} is not a valid evidence source:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(document)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def _load_one(path: Path, shown: str) -> EvidenceSource:
    """Parse and validate one source file. Raises ``ValueError`` with the
    message a :class:`SourceProblem` should carry."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{shown} could not be read: {exc.strerror}") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise ValueError(f"{shown} is not valid YAML{where}.") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{shown} must be a mapping with a 'kind' and a 'server' key; got "
            f"{type(raw).__name__}."
        )
    try:
        return EvidenceSource.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(shown, exc)) from None


def _resolve_results(
    source: EvidenceSource, base_dir: Path
) -> tuple[Path | None, str | None]:
    """The results path and, when it cannot be read now, why."""
    if source.results_env is not None:
        value = os.environ.get(source.results_env) or None
        if value is None:
            return None, f"results_env {source.results_env} is not set"
        written = value
    else:
        written = source.results or ""
    path = Path(written)
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        return path, f"results file not found: {written}"
    return path, None


def load_sources(base_dir: Path) -> SourcesLoad:
    """Every source under ``<base_dir>/.itest/sources/``, by file name.

    An absent directory is an empty load: a project that has never declared a
    source behaves exactly as it did before sources existed.
    """
    directory = sources_dir(base_dir)
    if not directory.is_dir():
        return SourcesLoad([], [])

    sources: list[LoadedSource] = []
    errors: list[SourceProblem] = []
    for path in sorted(directory.glob("*.yaml")):
        name = path.stem
        shown = source_path(name)
        try:
            source = _load_one(path, shown)
        except ValueError as exc:
            errors.append(SourceProblem(name=name, path=shown, message=str(exc)))
            continue
        results_path, problem = _resolve_results(source, base_dir)
        sources.append(
            LoadedSource(
                name=name, source=source, results_path=results_path, problem=problem
            )
        )
    return SourcesLoad(sources, errors)
