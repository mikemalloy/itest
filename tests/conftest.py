"""Suite-wide pytest configuration: marker registration only."""

from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "slow: builds or installs something (a wheel, a venv); seconds, not millis",
    )
