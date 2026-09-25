from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pmcup.trading_window import in_live_window, window_status


ET = ZoneInfo("America/New_York")


def test_before_open() -> None:
    t = datetime(2026, 9, 30, 12, 0, tzinfo=ET)
    assert in_live_window(t) is False
    assert window_status(t)["phase"] == "pre_open"


def test_during_open() -> None:
    t = datetime(2026, 10, 15, 15, 0, tzinfo=ET)
    assert in_live_window(t) is True
    assert window_status(t)["phase"] == "open"


def test_after_lock() -> None:
    t = datetime(2026, 11, 4, 12, 0, tzinfo=ET)
    assert in_live_window(t) is False
    assert window_status(t)["phase"] == "locked"
