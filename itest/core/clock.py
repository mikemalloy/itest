"""The wall clock, read through one function so a run's "now" can be pinned.

Sync stamps the manifest with its clock and decides evidence staleness against
it (``join.is_stale``). The syncer reads that moment here, and a test pins it
with ``monkeypatch.setattr(clock, "now", lambda: fixed)`` — so no test depends
on the calendar, and a dated fixture does not go stale by ageing.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    """The current time, timezone-aware UTC."""
    return datetime.now(UTC)
