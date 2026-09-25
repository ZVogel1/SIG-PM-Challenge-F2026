from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# Official Predictions Cup midterms window (Eastern Time)
LIVE_OPEN = datetime(2026, 10, 1, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
LIVE_CLOSE = datetime(2026, 11, 4, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))


def now_et(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(ZoneInfo("America/New_York"))
    if now.tzinfo is None:
        return now.replace(tzinfo=ZoneInfo("America/New_York"))
    return now.astimezone(ZoneInfo("America/New_York"))


def in_live_window(now: datetime | None = None) -> bool:
    """True only during the official cup trading window."""
    t = now_et(now)
    return LIVE_OPEN <= t < LIVE_CLOSE


def window_status(now: datetime | None = None) -> dict[str, str | bool]:
    t = now_et(now)
    if t < LIVE_OPEN:
        phase = "pre_open"
    elif t < LIVE_CLOSE:
        phase = "open"
    else:
        phase = "locked"
    return {
        "phase": phase,
        "in_window": phase == "open",
        "open_et": LIVE_OPEN.isoformat(),
        "close_et": LIVE_CLOSE.isoformat(),
        "now_et": t.isoformat(),
    }
