from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pmcup.config import Settings
from pmcup.guard import (
    check_churn,
    check_drawdown,
    check_open_orders,
    check_order_rate,
    check_rotation_budget,
    check_stalled,
    due_alerts,
    last_cycle_time,
    parse_orders,
)

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "bots").mkdir(parents=True)


def _cfg(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "supermarket_api_key": "test",
        "rotation_cooldown_hours": 12.0,
        "rotation_max_per_day": 3,
        "guard_max_stall_seconds": 900,
        "guard_max_orders_per_hour": 40,
        "guard_max_open_orders": 25,
        "guard_max_drawdown": 0.10,
        "bot_interval_seconds": 60,
    }
    base.update(over)
    return Settings(**base)


def _line(ts: datetime, action: str, title: str, *, qty: int = 100, px: float = 0.3) -> str:
    stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"{stamp},096 INFO pmcup.bots: [LIVE] edge_hunter {action} YES "
        f"x{qty} @ {px} — {title}"
    )


# --- log parsing ------------------------------------------------------------


def test_parses_real_order_line():
    raw = (
        "2026-10-08 17:01:46,096 INFO pmcup.bots: [LIVE] risk_manager SELL NO "
        "x15531 @ 0.32 — Will the Democratic Party win the PA-08 House race?"
    )
    (order,) = parse_orders([raw])
    assert order["bot"] == "risk_manager"
    assert order["action"] == "SELL"
    assert order["side"] == "NO"
    assert order["qty"] == 15531
    assert order["price"] == 0.32
    assert order["title"] == "Will the Democratic Party win the PA-08 House race?"


def test_ignores_non_order_lines():
    assert parse_orders(["2026-10-08 18:00:31,857 INFO pmcup.bots: Cycle 55 done"]) == []


def test_lookback_window_is_time_based_not_byte_based(tmp_path):
    """
    A fixed-size tail silently shrinks the window when the bot gets chatty,
    which is exactly when churn happens. The old 4MB read missed a sell/re-buy
    nine hours back because noise had pushed it past the byte cutoff.
    """
    from pmcup.guard import tail_lines

    log = tmp_path / "runner.log"
    old = NOW - timedelta(hours=9)
    noise = "2026-10-08 12:00:00,000 INFO pmcup.bots: Skip buy (cap leaves only 0 shares): x\n"
    with log.open("w") as fh:
        fh.write(_line(old, "SELL", "Will the D Party win IA-01?") + "\n")
        fh.write(_line(old + timedelta(seconds=1), "BUY", "Will the D Party win IA-01?") + "\n")
        fh.write(noise * 60_000)  # ~5MB, past the old 4MB cutoff

    assert log.stat().st_size > 4_000_000
    covered = parse_orders(tail_lines(log, since=NOW - timedelta(hours=12)))
    assert len(covered) == 2, "time-based lookback must reach past the byte cutoff"
    assert check_churn(covered, _cfg(), now=NOW) is not None


def test_reads_last_cycle_time():
    lines = [
        "2026-10-08 17:59:31,857 INFO pmcup.bots: Cycle 54 done | live=True",
        "2026-10-08 18:00:31,857 INFO pmcup.bots: Cycle 55 done | live=True",
    ]
    assert last_cycle_time(lines) == datetime(2026, 10, 8, 18, 0, 31, tzinfo=timezone.utc)


# --- churn: the check that matters ------------------------------------------


def test_buy_then_sell_is_not_churn():
    """A position that rose into its take-profit is a win, not a loop."""
    orders = parse_orders(
        [
            _line(NOW - timedelta(hours=6), "BUY", "Will the R Party win IA-01?"),
            _line(NOW - timedelta(hours=1), "SELL", "Will the R Party win IA-01?"),
        ]
    )
    assert check_churn(orders, _cfg(), now=NOW) is None


def test_sell_then_rebuy_is_churn():
    """The actual October loop: sold IA-01 at 0.150, re-bought at 0.155 a second later."""
    orders = parse_orders(
        [
            _line(NOW - timedelta(hours=2), "SELL", "Will the R Party win IA-01?", px=0.150),
            _line(NOW - timedelta(hours=2) + timedelta(seconds=1), "BUY",
                  "Will the R Party win IA-01?", px=0.155),
        ]
    )
    alert = check_churn(orders, _cfg(), now=NOW)
    assert alert is not None
    assert alert.key == "churn"
    assert "IA-01" in alert.detail


def test_rotation_across_different_markets_is_not_churn():
    """Selling one market to fund a different one is the whole point of rotation."""
    orders = parse_orders(
        [
            _line(NOW - timedelta(minutes=30), "SELL", "Will the R Party win Nevada Governor?"),
            _line(NOW - timedelta(minutes=29), "BUY", "Will the R Party win the PA-10 race?"),
        ]
    )
    assert check_churn(orders, _cfg(), now=NOW) is None


def test_rebuy_outside_the_cooldown_is_not_flagged():
    orders = parse_orders(
        [
            _line(NOW - timedelta(hours=40), "SELL", "Will the R Party win IA-01?"),
            _line(NOW - timedelta(hours=39), "BUY", "Will the R Party win IA-01?"),
        ]
    )
    assert check_churn(orders, _cfg(), now=NOW) is None


# --- rate and pile-up -------------------------------------------------------


def test_order_rate_under_limit_is_quiet():
    orders = parse_orders(
        [_line(NOW - timedelta(minutes=i), "BUY", f"market {i}") for i in range(10)]
    )
    assert check_order_rate(orders, _cfg(), now=NOW) is None


def test_runaway_order_rate_alerts():
    orders = parse_orders(
        [_line(NOW - timedelta(seconds=30 * i), "BUY", f"market {i}") for i in range(100)]
    )
    alert = check_order_rate(orders, _cfg(), now=NOW)
    assert alert is not None and alert.key == "order_rate"


def test_resting_order_pileup_alerts():
    assert check_open_orders(5, _cfg()) is None
    alert = check_open_orders(927, _cfg())
    assert alert is not None and "927" in alert.title


def test_open_orders_unknown_is_not_an_alert():
    assert check_open_orders(None, _cfg()) is None


# --- stall ------------------------------------------------------------------


def test_fresh_cycle_is_quiet():
    assert check_stalled(NOW - timedelta(minutes=2), _cfg(), now=NOW) is None


def test_stalled_cycles_alert():
    alert = check_stalled(NOW - timedelta(hours=3), _cfg(), now=NOW)
    assert alert is not None and alert.key == "stalled"


def test_no_cycles_at_all_alerts():
    assert check_stalled(None, _cfg(), now=NOW) is not None


# --- drawdown ---------------------------------------------------------------


def _pt(ts: datetime, cash: float, mv: float) -> dict[str, Any]:
    return {"ts": ts.isoformat(), "cash": cash, "positions_mv": mv}


def test_drawdown_within_tolerance_is_quiet():
    pts = [
        _pt(NOW - timedelta(hours=5), 0, 125_000),
        _pt(NOW, 0, 120_000),
    ]
    assert check_drawdown(pts, _cfg(), now=NOW) is None


def test_real_drawdown_alerts():
    pts = [
        _pt(NOW - timedelta(hours=5), 0, 158_844),
        _pt(NOW, 0, 124_602),
    ]
    alert = check_drawdown(pts, _cfg(), now=NOW)
    assert alert is not None and alert.key == "drawdown"


def test_unmarked_positions_do_not_fake_a_crash():
    """A cycle where the positions call failed stores bare cash; plotting it looked like a wipeout."""
    pts = [
        _pt(NOW - timedelta(hours=5), 0, 125_000),
        {"ts": (NOW - timedelta(hours=1)).isoformat(), "cash": 0.185, "positions_mv": None},
        _pt(NOW, 0, 124_900),
    ]
    assert check_drawdown(pts, _cfg(), now=NOW) is None


# --- rotation budget --------------------------------------------------------


def test_rotation_within_budget_is_quiet():
    state = {"rotations": {NOW.date().isoformat(): 3}}
    assert check_rotation_budget(state, _cfg(), now=NOW) is None


def test_rotation_over_budget_alerts():
    state = {"rotations": {NOW.date().isoformat(): 9}}
    alert = check_rotation_budget(state, _cfg(), now=NOW)
    assert alert is not None and alert.key == "rotation_budget"


# --- send cooldown ----------------------------------------------------------


def test_same_alert_does_not_mail_every_pass():
    cfg = _cfg(guard_alert_cooldown_s=3600)
    alerts = [check_open_orders(927, cfg)]
    first, state = due_alerts(alerts, cfg, now=NOW, state={})
    assert len(first) == 1
    second, state = due_alerts(alerts, cfg, now=NOW + timedelta(minutes=5), state=state)
    assert second == []
    later, _ = due_alerts(alerts, cfg, now=NOW + timedelta(hours=2), state=state)
    assert len(later) == 1


def test_cleared_condition_can_alert_again_immediately():
    cfg = _cfg(guard_alert_cooldown_s=86400)
    alert = check_open_orders(927, cfg)
    _, state = due_alerts([alert], cfg, now=NOW, state={})
    # condition clears...
    _, state = due_alerts([], cfg, now=NOW + timedelta(minutes=5), state=state)
    # ...and comes back; the user should hear about it rather than wait a day
    again, _ = due_alerts([alert], cfg, now=NOW + timedelta(minutes=10), state=state)
    assert len(again) == 1
