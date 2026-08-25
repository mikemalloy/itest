"""A tiny pytest plugin that records per-test outcomes to a JSON file.

``itest verify`` runs pytest in a subprocess with this plugin enabled
(``-p itest.core._pytest_report``) and reads the resulting JSON rather than
scraping pytest's text output. The output path is taken from the
``ITEST_REPORT`` environment variable.

The document has two sections. ``tests`` is keyed by test node id and holds
per-test outcomes. ``collection_errors`` is keyed by the *collector's* node id
— usually a file — and holds modules pytest could not even import. Those never
produce a per-test report, so without recording them separately every test in a
broken module would look merely absent, and verify would call it a stub.
"""

from __future__ import annotations

import json
import os

_RESULTS: dict[str, dict] = {}
_COLLECTION_ERRORS: dict[str, dict] = {}


def pytest_runtest_logreport(report) -> None:
    # The "call" phase is the test body: passed / failed / skipped-in-body.
    if report.when == "call":
        _RESULTS[report.nodeid] = {
            "outcome": report.outcome,
            "detail": report.longreprtext if report.failed else "",
        }
    # A skip during setup (e.g. skipif) never reaches the call phase.
    elif report.when == "setup" and report.outcome == "skipped":
        _RESULTS.setdefault(report.nodeid, {"outcome": "skipped", "detail": ""})
    # An error during setup should surface as a failure.
    elif report.when == "setup" and report.outcome == "failed":
        _RESULTS[report.nodeid] = {
            "outcome": "failed",
            "detail": report.longreprtext,
        }


def pytest_collectreport(report) -> None:
    """Record a module or directory pytest could not collect (e.g. bad import)."""
    if report.failed:
        _COLLECTION_ERRORS[report.nodeid] = {
            "outcome": "error",
            "detail": report.longreprtext,
        }


def pytest_sessionfinish(session, exitstatus) -> None:
    path = os.environ.get("ITEST_REPORT")
    if path:
        document = {"tests": _RESULTS, "collection_errors": _COLLECTION_ERRORS}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(document, fh)
