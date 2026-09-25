"""Join a run's evidence onto the manifest's tool points.

The key is ``(source.server, tool name)`` and nothing else: a point whose
``source`` is the server and whose ``target`` is the tool, of type
``mcp_tool``. Matched → an :class:`~itest.core.manifest.EvidenceRecord`.
Unmatched → the source line's ``unmatched_tools``. A server the manifest does
not inventory → ``server_not_declared``, zero records, and the run identity
still on the source line so the reader can see what was ignored.

Staleness is decided here, once, with the sync's clock: a run older than the
source's ``max_age_days`` is ``stale`` on every record it produced. Stored,
never recomputed at report time — the report shows what sync knew.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from itest.core.evidence.loader import LoadedSource
from itest.core.evidence.promptfoo import RunEvidence
from itest.core.manifest import EvidenceRecord, Manifest, SourceRecord

#: The declared point type. Spelled out so the join never imports the
#: declarations package's probe side.
_TOOL_POINT_TYPE = "mcp_tool"


def parse_run_at(value: str | None) -> datetime | None:
    """A promptfoo timestamp (``2026-09-23T18:26:29.565Z``) as an aware
    datetime, or ``None`` when absent or unreadable."""
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_stale(run_at: str | None, now: datetime, max_age_days: int) -> bool:
    """``(now − run_at) > max_age_days``. An undated run is stale: it cannot
    be shown as fresh evidence."""
    started = parse_run_at(run_at)
    if started is None:
        return True
    return now - started > timedelta(days=max_age_days)


def unreadable_source(source: LoadedSource, reason: str) -> SourceRecord:
    """The source line for a results file that could not be read."""
    return SourceRecord(
        name=source.name,
        kind=source.source.kind,
        server=source.source.server,
        status="unreadable",
        reason=reason,
        agent=source.source.agent,
        standards=list(source.source.standards),
    )


def join_evidence(
    run: RunEvidence, source: LoadedSource, manifest: Manifest, *, now: datetime
) -> tuple[list[EvidenceRecord], SourceRecord]:
    """The records for every matched tool, and the source line."""
    server = source.source.server
    points = {
        p.target: p
        for p in manifest.points
        if p.type == _TOOL_POINT_TYPE and p.source == server
    }
    line = SourceRecord(
        name=source.name,
        kind=run.kind,
        server=server,
        run_id=run.run_id,
        run_at=run.run_at,
        status="read",
        shape=run.shape,
        rows=run.rows,
        agent=source.source.agent,
        standards=list(source.source.standards),
        notes=list(run.notes),
    )
    if not points:
        line.status = "server_not_declared"
        line.reason = (
            f"server {server!r} is not a declared server the manifest "
            "inventories; nothing joined"
        )
        return [], line

    stale = is_stale(run.run_at, now, source.source.max_age_days)
    line.stale = stale
    records: list[EvidenceRecord] = []
    for tool, evidence in run.per_tool.items():
        point = points.get(tool)
        if point is None:
            line.unmatched_tools.append(tool)
            continue
        line.matched_tools.append(tool)
        records.append(
            EvidenceRecord(
                point_id=point.id,
                source_name=source.name,
                kind=run.kind,
                run_id=run.run_id,
                run_at=run.run_at,
                tool_version=run.tool_version,
                agent=source.source.agent,
                stale=stale,
                calls=evidence.calls,
                refused=evidence.refused,
                succeeded=evidence.succeeded,
                rows_total=evidence.rows_total,
                rows_with_call=evidence.rows_with_call,
                targeted=evidence.targeted,
                targeted_rows_with_call=evidence.targeted_rows_with_call,
                targeted_rows_refused=evidence.targeted_rows_refused,
                targeted_rows_succeeded=evidence.targeted_rows_succeeded,
                recorded_at=now,
            )
        )
    run.unmatched_tools = list(line.unmatched_tools)
    return records, line
