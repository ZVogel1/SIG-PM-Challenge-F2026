from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pmcup.equity import downsample, equity_series, render_equity_svg


def _row(minutes_ago: int, cash: float, mv: float | None, live: bool) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "ts": ts.isoformat(),
        "equity": cash if not live else cash + (mv or 0),
        "cash": cash,
        "positions_mv": mv,
        "paper_pnl": 0.0,
        "live": live,
    }


def test_equity_ignores_live_flag_and_counts_positions() -> None:
    # The same portfolio logged twice, once with live=False — both must match
    rows = [_row(10, 7_554.77, 126_879.48, False), _row(5, 7_554.77, 126_879.48, True)]
    s = equity_series(rows)
    assert s["ys"] == [134_434.25, 134_434.25]


def test_sample_without_position_mark_is_dropped() -> None:
    # Positions call failed: bare cash is not equity
    rows = [
        _row(10, 100_000.0, 20_000.0, True),
        _row(5, 0.185, None, True),
        _row(1, 100_000.0, 21_000.0, True),
    ]
    s = equity_series(rows)
    assert s["count"] == 2
    assert s["low"] == 120_000.0


def test_chart_axis_is_time_proportional() -> None:
    # A long gap then a short one — the drawn x spacing must reflect that
    rows = [
        _row(600, 100_000.0, 0.0, True),
        _row(10, 110_000.0, 0.0, True),
        _row(0, 120_000.0, 0.0, True),
    ]
    svg = render_equity_svg(equity_series(rows), width=900, height=280)
    pts = svg.split('stroke-linecap="round" points="')[1].split('"')[0].split()
    x0, x1, x2 = (float(p.split(",")[0]) for p in pts)
    assert (x1 - x0) > (x2 - x1) * 10


def test_downsample_keeps_extremes() -> None:
    base = datetime.now(timezone.utc)
    xs = [base + timedelta(minutes=i) for i in range(1000)]
    ys = [100.0] * 1000
    ys[500] = 999.0
    ys[700] = 1.0
    _, out = downsample(xs, ys, max_points=50)
    assert 999.0 in out and 1.0 in out
