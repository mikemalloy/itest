"""Manifest schema and YAML load/save round-trip.

The manifest (`.itest/manifest.yaml`) is ITest's single shared artifact: the
inventory of detected integration points and the registry of tests that cover
them. It must stay human-readable and diffable, so YAML is emitted with fields
in declaration order and no key sorting.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

SCHEMA_VERSION = 2

Tier = Literal["static", "readonly", "active"]


class IntegrationPoint(BaseModel):
    """A single primitive integration point.

    Emitted by a detector reading Terraform, or — for ``mcp_tool`` — built from a
    declaration plus the live tool listing it describes (``origin: declared``).
    A declared point carries the declaration file in ``hcl_address``: it is the
    document the point came from, which is what the field means either way.

    Adding ``mcp_tool`` needs no schema bump: it is a new value of an existing
    field, every attribute rides in the free-form ``attributes`` dict, and an
    existing manifest therefore loads unchanged.
    """

    id: str
    type: Literal[
        "sg_edge", "iam_edge", "event_edge", "route_edge", "lb_edge", "mcp_tool"
    ]
    source: str
    target: str
    attributes: dict = Field(default_factory=dict)
    #: ``mcp_tool`` only: the trait ids the last sync decided apply to this
    #: tool, recomputed from the current table on every sync. ``None`` on every
    #: other point type, and on a tool no sync has recorded yet — omitted from
    #: the YAML then, so a declaration-free manifest is byte-identical.
    traits_planned: list[str] | None = None
    hcl_address: str
    origin: Literal["detected", "declared"] = "detected"
    first_seen: datetime
    last_seen: datetime


class TestEntry(BaseModel):
    """A registered test and its relationship to an integration point."""

    # Not a pytest test class despite the name prefix.
    __test__ = False

    id: str
    point_id: str
    path: str
    test_name: str
    #: The trait this test checks, for a declared tool's per-trait test.
    #: ``None`` for a detected point's test and a hand-registered one.
    trait: str | None = None
    ownership_hash: str
    status: Literal["stub", "implemented", "orphaned"] = "stub"
    #: The trait no longer applies to the tool (the table or the tool changed).
    #: A retired test is kept on disk and in the manifest, never run, and
    #: reported as ``not_applicable``; it is restored, under the same id, if
    #: the trait applies again. Distinct from ``orphaned``: the point is alive.
    retired: bool = False
    disabled: bool = False
    disabled_reason: str | None = None
    labels: list[str] = Field(default_factory=list)
    # --- v2 scheduling fields (schema only; no runner uses them yet) ---
    #: Execution class: static (no AWS calls), readonly (describe/get only),
    #: active (mutating probes). Also the future concurrency class.
    tier: Tier = "readonly"
    #: Serialization key for the future parallel runner: tests sharing a
    #: resource_group must not run concurrently. Defaults to the point's
    #: target identity at sync time.
    resource_group: str | None = None
    #: Wall-clock seconds of this test's last run, recorded by verify.
    last_duration_seconds: float | None = None

    @property
    def canonical(self) -> str:
        """Canonical test address: ``path::test_name``."""
        return f"{self.path}::{self.test_name}"


class CoverageSummary(BaseModel):
    """Point-level coverage counts derived from the manifest alone.

    "Covered" means a point has at least one enabled, non-orphaned test. Pass
    /fail counts are runtime state and computed elsewhere (the verifier).
    """

    total_points: int
    covered: int
    uncovered: int


class Manifest(BaseModel):
    """The full manifest document."""

    schema_version: int = SCHEMA_VERSION
    generated_at: datetime
    #: ``trait_table_hash()`` of the table the last sync planned declared tools
    #: against. ``None`` (and omitted) until a declared tool exists.
    trait_table_hash: str | None = None
    points: list[IntegrationPoint] = Field(default_factory=list)
    tests: list[TestEntry] = Field(default_factory=list)

    def get_point(self, point_id: str) -> IntegrationPoint | None:
        """Return the point with ``point_id``, or ``None``."""
        for point in self.points:
            if point.id == point_id:
                return point
        return None

    def tests_for_point(self, point_id: str) -> list[TestEntry]:
        """Return all tests registered against ``point_id``."""
        return [t for t in self.tests if t.point_id == point_id]

    def coverage_summary(self) -> CoverageSummary:
        """Compute point-level coverage from the manifest."""
        covered = 0
        for point in self.points:
            has_live_test = any(
                (not t.disabled) and (not t.retired) and t.status != "orphaned"
                for t in self.tests_for_point(point.id)
            )
            if has_live_test:
                covered += 1
        total = len(self.points)
        return CoverageSummary(
            total_points=total,
            covered=covered,
            uncovered=total - covered,
        )


def load_manifest(path: str | Path) -> Manifest:
    """Load and validate a manifest from ``path``.

    Raises ``ValueError`` if the file declares a schema version newer than this
    build understands.
    """
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    version = data.get("schema_version")
    if isinstance(version, int) and version > SCHEMA_VERSION:
        raise ValueError(
            f"Manifest schema_version {version} is newer than this build "
            f"supports (max {SCHEMA_VERSION}). Upgrade ITest to read it."
        )

    manifest = Manifest.model_validate(data)
    if isinstance(version, int) and version < SCHEMA_VERSION:
        _migrate(manifest, version)
    return manifest


def default_resource_group(manifest: Manifest, entry: TestEntry) -> str | None:
    """The point's target identity — what a test on it would contend for."""
    point = manifest.get_point(entry.point_id)
    return point.target if point else None


def _migrate(manifest: Manifest, from_version: int) -> None:
    """Upgrade an older manifest in place. Saving then writes the new version.

    v1 -> v2: TestEntry gains tier (default readonly), resource_group (filled
    from the point's target), last_duration_seconds (unknown until verify).
    Pydantic already applied the defaults; only resource_group needs data.
    """
    if from_version < 2:
        for entry in manifest.tests:
            if entry.resource_group is None:
                entry.resource_group = default_resource_group(manifest, entry)
    manifest.schema_version = SCHEMA_VERSION


#: Fields written only when they differ from their default. All of them arrived
#: with the live trait table and mean something only for a declared tool, so a
#: manifest without one — every Terraform-only project — serializes exactly as
#: it did before they existed. No schema bump: each loads as its default.
_SPARSE_TOP = {"trait_table_hash": None}
_SPARSE_POINT = {"traits_planned": None}
_SPARSE_TEST = {"trait": None, "retired": False}


def _sparse(data: dict) -> dict:
    def drop(document: dict, defaults: dict) -> None:
        for key, default in defaults.items():
            if key in document and document[key] == default:
                del document[key]

    drop(data, _SPARSE_TOP)
    for point in data.get("points", []):
        drop(point, _SPARSE_POINT)
    for test in data.get("tests", []):
        drop(test, _SPARSE_TEST)
    return data


def save_manifest(manifest: Manifest, path: str | Path) -> None:
    """Write ``manifest`` to ``path`` as diffable YAML (fields in order).

    Atomic: the YAML is written to a temp file in the same directory and then
    ``os.replace``-d into place, so a concurrent reader (or another `itest`
    process) never sees a half-written manifest, and a crash mid-write leaves
    the previous manifest intact rather than truncating it to empty.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _sparse(manifest.model_dump(mode="json"))
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
