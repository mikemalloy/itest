"""The standards view: the same checks, seen through the frameworks a security
reader already reports against.

The trait families (authority / blast / containment / change) are the skeleton:
a clean partition, they carry the tier semantics, and they do not get
renumbered when OWASP publishes an edition. A trait maps to *several* published
ids, so standards cannot be the structure — they are a second lens over the
same data. This module is that lens:

- :func:`load_standards` reads the shipped catalogue (``standards.yaml``): every
  id's human title, and for the OWASP Agentic Top 10 ITest's own scope claim
  (covered / partial / not covered, with a one-line reason).
- :func:`standards_rollup` derives, from a ledger's checks, one entry per id
  any check cites — how many checks, and how many per status — and adds every
  ASI entry nothing cites as ``not_covered``. Derived at emit time, never a
  hand-maintained second list, so the counts cannot drift from the checks.
- :func:`render_rollup` is what ``itest standards`` prints.

Naming what is NOT covered is deliberate. A tool that prints "not covered"
beside five rows is more credible than one that stays silent and lets a reader
assume coverage. The LLM Top 10 rows appear only where a check cites them: that
list is a neighbouring framework, not ITest's coverage claim.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib import resources
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CATALOG_PACKAGE = "itest.traits"
CATALOG_RESOURCE = "standards.yaml"
CATALOG_VERSION = 1

#: Frameworks in the order the rollup lists them; an id outside every one
#: sorts last, under ``other``.
FRAMEWORK_ORDER = ("ASI", "LLM", "semgrep", "CWE", "ACS", "other")

#: The statuses the rollup counts, in the order they are printed. ``n/a`` in a
#: check is counted as ``not_applicable``.
STATUSES = (
    "pass",
    "fail",
    "critical",
    "changed",
    "not_verifiable",
    "not_applicable",
    "held_out",
    "not_run",
)
_STATUS_ALIASES = {"n/a": "not_applicable"}

Coverage = Literal["covered", "partial", "not_covered"]

#: What an uncited id whose catalogue claim is covered or partial says.
UNCITED_NOTE = "no check in this run cites it"

#: Lifecycle states that do not count as this run's evidence — the same filter
#: the families rollup applies. A retired or orphaned check cites nothing:
#: coverage is a statement about this run, and a standard cited only by a
#: check that no longer applies must not read "covered".
UNCOUNTED_STATES = frozenset({"not_applicable", "orphan"})


def counted_checks(checks: Iterable[dict]) -> list[dict]:
    """The checks a rollup may cite: every one whose state is not retired or
    orphaned."""
    return [c for c in checks if c.get("state") not in UNCOUNTED_STATES]


class StandardsCatalogError(Exception):
    """A catalogue that cannot be read."""


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    #: ITest's scope claim, for frameworks that carry one (the ASI list).
    coverage: Coverage | None = None
    note: str | None = None


class Framework(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    #: ``every``: each entry appears in the rollup, uncited ones as not covered.
    #: ``cited``: an entry appears only when a check cites it.
    rollup: Literal["every", "cited"]
    entries: list[Entry] = Field(default_factory=list)


class StandardsCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = CATALOG_VERSION
    frameworks: list[Framework] = Field(default_factory=list)

    def framework(self, framework_id: str) -> Framework:
        for framework in self.frameworks:
            if framework.id == framework_id:
                return framework
        raise KeyError(framework_id)

    def entry(self, standard_id: str) -> Entry | None:
        for framework in self.frameworks:
            for entry in framework.entries:
                if entry.id == standard_id:
                    return entry
        return None

    def title(self, standard_id: str) -> str:
        """The id's title, or the id itself: a title is never invented."""
        entry = self.entry(standard_id)
        return entry.title if entry is not None else standard_id


def load_standards() -> StandardsCatalog:
    """The shipped catalogue, read through :mod:`importlib.resources`."""
    path = resources.files(CATALOG_PACKAGE).joinpath(CATALOG_RESOURCE)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise StandardsCatalogError(f"{path} could not be read: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != CATALOG_VERSION:
        raise StandardsCatalogError(
            f"{path} is not a version {CATALOG_VERSION} standards catalogue."
        )
    try:
        return StandardsCatalog.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError, rendered with the path
        raise StandardsCatalogError(f"{path} is not a valid catalogue: {exc}") from None


def framework_of(standard_id: str) -> str:
    """Which framework an id belongs to, by its prefix."""
    if standard_id.startswith("ASI"):
        return "ASI"
    if standard_id.startswith("LLM"):
        return "LLM"
    if standard_id.startswith("semgrep-"):
        return "semgrep"
    if standard_id.startswith("CWE-"):
        return "CWE"
    if standard_id.startswith("ACS-"):
        return "ACS"
    return "other"


def _sort_key(standard_id: str) -> tuple:
    """Framework order, then numerically within a framework where the id has a
    number (``CWE-77`` before ``CWE-306``; ``semgrep-server-4`` before ``-18``)."""
    framework = framework_of(standard_id)
    tail = standard_id.rsplit("-", 1)[-1] if "-" in standard_id else standard_id[3:]
    number = int(tail) if tail.isdigit() else 10**6
    return (
        FRAMEWORK_ORDER.index(framework),
        standard_id.rsplit("-", 1)[0],
        number,
        standard_id,
    )


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(STATUSES, 0)


def standards_rollup(
    checks: Iterable[dict], catalog: StandardsCatalog | None = None
) -> list[dict]:
    """One entry per published id, derived from ``checks``.

    Each check is a ledger row (``standards``, ``status``). An id any check
    cites carries the number of checks and the count by status. Every entry of
    a ``rollup: every`` framework (the ASI list) appears even when nothing
    cites it, as ``not_covered`` with the catalogue's reason. Coverage is a
    statement about this run's evidence: an id the catalogue claims but no
    check cites is ``not_covered`` too, and one the catalogue declines to
    claim (ASI01) stays ``not_covered`` however many checks cite it, with its
    counts shown so the reader can see why.
    """
    catalog = catalog if catalog is not None else load_standards()
    cited: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for check in checks:
        status = str(check.get("status") or "")
        status = _STATUS_ALIASES.get(status, status)
        for standard_id in check.get("standards") or []:
            counts = cited.setdefault(standard_id, _empty_counts())
            totals[standard_id] = totals.get(standard_id, 0) + 1
            if status in counts:
                counts[status] += 1

    ids = set(cited)
    for framework in catalog.frameworks:
        if framework.rollup == "every":
            ids.update(entry.id for entry in framework.entries)

    rollup = []
    for standard_id in sorted(ids, key=_sort_key):
        entry = catalog.entry(standard_id)
        checks_here = totals.get(standard_id, 0)
        claim = entry.coverage if entry is not None else None
        note = entry.note if entry is not None else None
        if checks_here == 0:
            coverage = "not_covered"
            if claim != "not_covered":
                note = UNCITED_NOTE
        elif claim is None:
            coverage = "covered"
        else:
            coverage = claim
        rollup.append(
            {
                "id": standard_id,
                "title": catalog.title(standard_id),
                "framework": framework_of(standard_id),
                "coverage": coverage,
                "note": note if coverage != "covered" else None,
                "checks": checks_here,
                "statuses": cited.get(standard_id, _empty_counts()),
            }
        )
    return rollup


def rollup_for_ledger(ledger: dict | None) -> list[dict]:
    """The rollup a verify JSON ``tools`` section carries, or — for one written
    before the rollup existed — the same thing derived from its checks."""
    if not ledger:
        return []
    if ledger.get("standards"):
        return list(ledger["standards"])
    checks = [
        check
        for server in ledger.get("servers") or []
        for tool in server.get("tools") or []
        for check in tool.get("checks") or []
    ]
    return standards_rollup(counted_checks(checks))


# --- rendering ------------------------------------------------------------------

COVERAGE_LABEL = {
    "covered": "covered",
    "partial": "partial",
    "not_covered": "not covered",
}

_COLUMNS = ("PASS", "FAIL", "CRITICAL", "CHANGED", "NOT VERIFIABLE", "N/A")
_COLUMN_STATUS = (
    "pass",
    "fail",
    "critical",
    "changed",
    "not_verifiable",
    "not_applicable",
)


def _columns(rows: list[list[str]], indent: str = "  ") -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        (
            indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        ).rstrip()
        for row in rows
    ]


def render_rollup(rollup: list[dict], ledger: dict | None) -> str:
    """What ``itest standards`` prints: the rollup as a table, one line per id,
    the coverage stated plainly and a not-covered or partial row's reason
    beneath it."""
    if not ledger:
        return (
            "No agent tools declared in this run, so there is nothing to see "
            "through a standard. Declare an MCP server under .itest/tools/ and "
            "run itest verify."
        )
    servers = ledger.get("servers") or []
    checks = sum(
        len(t.get("checks") or []) for s in servers for t in s.get("tools") or []
    )
    cited = sum(1 for entry in rollup if entry["checks"])
    environments = sorted(
        {s.get("environment") for s in servers if s.get("environment")}
    )
    where = f" ({', '.join(environments)})" if environments else ""
    out = [
        f"Standards view: {checks} check(s) on {len(servers)} server(s){where}, "
        f"{cited} published id(s) cited.",
        "",
    ]
    rows = [["ID", "TITLE", "COVERAGE", "CHECKS", *_COLUMNS]]
    notes: dict[int, str] = {}
    for entry in rollup:
        rows.append(
            [
                entry["id"],
                entry["title"],
                COVERAGE_LABEL[entry["coverage"]],
                str(entry["checks"]),
                *(str(entry["statuses"].get(status, 0)) for status in _COLUMN_STATUS),
            ]
        )
        if entry.get("note"):
            notes[len(rows) - 1] = entry["note"]
    lines = _columns(rows)
    for index, line in enumerate(lines):
        out.append(line)
        if index in notes:
            out.append(f"      — {notes[index]}")
    out += [
        "",
        "Coverage is a statement about this run's evidence: an id no check cites "
        "is not covered, whatever the table could cite. The trait families "
        "(itest traits) remain the structure; this is a second lens over the same "
        "checks.",
    ]
    return "\n".join(out)
