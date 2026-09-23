"""Declared evidence sources: ``.itest/sources/<name>.yaml``.

A source names a red-team tool's results file and the declared server it was
run against. It is read-only input: ITest never runs the tool, and nothing in
the file is a credential — a results path is a path. The schema mirrors the
declaration schema's rules (unknown keys refused, exactly one of ``results`` /
``results_env``, standards ids validated against the same prefixes the trait
table accepts), and the loader mirrors the declaration loader's: every file
under the directory is read, and one bad file is a structured error beside the
good ones, never an exception that hides them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from itest.core.evidence.loader import load_sources, sources_dir
from itest.core.evidence.schema import EvidenceSource

VALID = {
    "kind": "promptfoo",
    "server": "reference-mcp",
    "results": "evidence/promptfoo.json",
    "agent": "sonnet-5 via agent.js",
    "standards": ["ASI01", "LLM01"],
    "max_age_days": 7,
}


def _write(base_dir: Path, name: str, document: dict | str) -> Path:
    path = sources_dir(base_dir) / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    path.write_text(text, encoding="utf-8")
    return path


# --- the schema -------------------------------------------------------------------


def test_a_valid_source_loads() -> None:
    source = EvidenceSource.model_validate(VALID)
    assert source.kind == "promptfoo"
    assert source.server == "reference-mcp"
    assert source.results == "evidence/promptfoo.json"
    assert source.results_env is None
    assert source.agent == "sonnet-5 via agent.js"
    assert source.standards == ["ASI01", "LLM01"]
    assert source.max_age_days == 7


def test_defaults_are_the_least_claim() -> None:
    source = EvidenceSource.model_validate(
        {"kind": "promptfoo", "server": "reference-mcp", "results": "r.json"}
    )
    assert source.agent is None
    assert source.standards == []
    assert source.max_age_days == 7


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EvidenceSource.model_validate({**VALID, "dangerous": True})
    assert "dangerous" in str(excinfo.value)


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        EvidenceSource.model_validate({**VALID, "kind": "garak"})


def test_both_results_and_results_env_are_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EvidenceSource.model_validate({**VALID, "results_env": "PROMPTFOO_RESULTS"})
    assert "exactly one" in str(excinfo.value)


def test_neither_results_nor_results_env_is_refused() -> None:
    document = {k: v for k, v in VALID.items() if k != "results"}
    with pytest.raises(ValidationError) as excinfo:
        EvidenceSource.model_validate(document)
    assert "exactly one" in str(excinfo.value)


def test_results_env_must_be_a_variable_name() -> None:
    document = {k: v for k, v in VALID.items() if k != "results"}
    with pytest.raises(ValidationError) as excinfo:
        EvidenceSource.model_validate({**document, "results_env": "/tmp/x.json"})
    assert "NAME" in str(excinfo.value)


def test_an_unknown_standards_prefix_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EvidenceSource.model_validate({**VALID, "standards": ["ASI01", "NIST-1"]})
    assert "NIST-1" in str(excinfo.value)
    assert "ASI" in str(excinfo.value)


def test_a_server_name_follows_the_declaration_rule() -> None:
    with pytest.raises(ValidationError):
        EvidenceSource.model_validate({**VALID, "server": "Reference MCP"})


def test_max_age_days_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        EvidenceSource.model_validate({**VALID, "max_age_days": -1})


# --- the loader -------------------------------------------------------------------


def test_no_sources_directory_means_no_sources(tmp_path: Path) -> None:
    loaded = load_sources(tmp_path)
    assert loaded.sources == []
    assert loaded.errors == []


def test_a_valid_file_resolves_its_results_path(tmp_path: Path) -> None:
    results = tmp_path / "evidence" / "promptfoo.json"
    results.parent.mkdir()
    results.write_text("{}", encoding="utf-8")
    _write(tmp_path, "promptfoo-lab", VALID)

    loaded = load_sources(tmp_path)
    assert loaded.errors == []
    (source,) = loaded.sources
    assert source.name == "promptfoo-lab"
    assert source.source.server == "reference-mcp"
    assert source.results_path == results
    assert source.problem is None


def test_results_env_is_resolved_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "ci-results.json"
    results.write_text("{}", encoding="utf-8")
    document = {k: v for k, v in VALID.items() if k != "results"}
    _write(tmp_path, "ci", {**document, "results_env": "PROMPTFOO_RESULTS"})
    monkeypatch.setenv("PROMPTFOO_RESULTS", str(results))

    (source,) = load_sources(tmp_path).sources
    assert source.results_path == results
    assert source.problem is None


def test_an_unset_results_env_is_a_problem_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {k: v for k, v in VALID.items() if k != "results"}
    _write(tmp_path, "ci", {**document, "results_env": "PROMPTFOO_RESULTS"})
    monkeypatch.delenv("PROMPTFOO_RESULTS", raising=False)

    (source,) = load_sources(tmp_path).sources
    assert source.results_path is None
    assert source.problem == "results_env PROMPTFOO_RESULTS is not set"


def test_a_missing_results_file_is_a_problem_not_an_exception(tmp_path: Path) -> None:
    _write(tmp_path, "promptfoo-lab", VALID)

    loaded = load_sources(tmp_path)
    assert loaded.errors == []
    (source,) = loaded.sources
    assert source.results_path == tmp_path / "evidence" / "promptfoo.json"
    assert source.problem is not None
    assert "evidence/promptfoo.json" in source.problem
    assert "not found" in source.problem


def test_a_bad_file_is_a_structured_error_beside_the_good_ones(tmp_path: Path) -> None:
    results = tmp_path / "evidence" / "promptfoo.json"
    results.parent.mkdir()
    results.write_text("{}", encoding="utf-8")
    _write(tmp_path, "good", VALID)
    _write(tmp_path, "typo", {**VALID, "resutls": "x.json"})
    _write(tmp_path, "broken", "kind: [promptfoo\n")

    loaded = load_sources(tmp_path)
    assert [s.name for s in loaded.sources] == ["good"]
    assert [e.name for e in loaded.errors] == ["broken", "typo"]
    typo = next(e for e in loaded.errors if e.name == "typo")
    assert "resutls" in typo.message
    assert typo.path.endswith(".itest/sources/typo.yaml")
    broken = next(e for e in loaded.errors if e.name == "broken")
    assert "not valid YAML" in broken.message


def test_a_source_file_that_is_not_a_mapping_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "list", "- promptfoo\n")
    loaded = load_sources(tmp_path)
    assert loaded.sources == []
    (error,) = loaded.errors
    assert error.name == "list"
    assert "mapping" in error.message
