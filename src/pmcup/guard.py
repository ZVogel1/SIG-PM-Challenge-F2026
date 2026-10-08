"""
Always-on anomaly watch.

The existing alerts in notify.py fire on crashes, failed cycles and rejected
orders. Every incident that actually cost money this cup was silent by that
standard — the duplicate-order pile-up and the cash-buffer churn loop both
looked like long runs of perfectly successful cycles. These checks watch for
the *shape* of those failures instead of for errors.

Runs as its own process, deliberately: if the trading daemon wedges or dies,
the guard is what is left to say so.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings, settings
from .equity import _repaired_equity, load_equity_points, parse_ts
from .notify import send_email

log = logging.getLogger("pmcup.guard")

RUNNER_LOG = Path("data/bots/runner.log")
STATE_PATH = Path("data/bots/guard_state.json")
ROTATION_PATH = Path("data/bots/rotation.json")

# 2026-10-08 17:01:46,096 INFO pmcup.bots: [LIVE] risk_manager SELL NO x15531 @ 0.32 — Will ...
_ORDER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*?"
    r"\[LIVE\] (?P<bot>\S+) (?P<action>BUY|SELL) (?P<side>YES|NO) "
    r"x(?P<qty>[\d.]+) @ (?P<price>[\d.]+) [—-] (?P<title>.+?)\s*$"
)
_CYCLE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*?Cycle (?P<n>\d+) done"
)


@dataclass(frozen=True)
class Alert:
    key: str  # stable id, used for the per-alert send cooldown
    title: str
    detail: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_log_ts(raw: str) -> datetime:
    """Runner logs are written in the VM's clock, which runs UTC."""
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _first_ts(lines: list[str]) -> datetime | None:
    for line in lines:
        if len(line) > 19 and line[4] == "-" and line[10] == " ":
            try:
                return _parse_log_ts(line[:19])
            except ValueError:
                continue
    return None


def _read_tail(path: Path, nbytes: int) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
            fh.readline()  # drop the partial first line
        return fh.read().decode("utf-8", "replace").splitlines()


def tail_lines(
    path: Path = RUNNER_LOG,
    *,
    since: datetime | None = None,
    max_bytes: int = 256_000_000,
) -> list[str]:
    """
    Read back far enough to actually cover `since`.

    Reading a fixed number of bytes looks equivalent but is not: the runner
    writes ~40 lines per cycle, so a fixed window silently shrinks whenever the
    bot gets chatty — which is exactly when churn happens. Grow the read until
    the oldest line is older than the cutoff, so the window is defined in time.
    """
    if not path.exists():
        return []
    size = path.stat().st_size
    nbytes = 4_000_000
    while True:
        lines = _read_tail(path, nbytes)
        if since is None or nbytes >= min(size, max_bytes):
            return lines
        oldest = _first_ts(lines)
        if oldest is not None and oldest <= since:
            return lines
        nbytes *= 4


def parse_orders(lines: Iterable[str], *, since: datetime | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines:
        m = _ORDER_RE.match(line)
        if not m:
            continue
        ts = _parse_log_ts(m.group("ts"))
        if since is not None and ts < since:
            continue
        out.append(
            {
                "ts": ts,
                "bot": m.group("bot"),
                "action": m.group("action"),
                "side": m.group("side"),
                "qty": float(m.group("qty")),
                "price": float(m.group("price")),
                "title": m.group("title"),
            }
        )
    return out


def last_cycle_time(lines: Iterable[str]) -> datetime | None:
    last: datetime | None = None
    for line in lines:
        m = _CYCLE_RE.match(line)
        if m:
            last = _parse_log_ts(m.group("ts"))
    return last


# --- checks -----------------------------------------------------------------
# Each takes already-gathered data and returns an Alert or None, so they can be
# tested without a VM, a log file or a network call.


def check_stalled(last_cycle: datetime | None, cfg: Settings, *, now: datetime) -> Alert | None:
    limit = float(cfg.guard_max_stall_seconds)
    if last_cycle is None:
        return Alert(
            "stalled",
            "Bot has never completed a cycle",
            "No 'Cycle N done' line found in the runner log at all.",
        )
    age = (now - last_cycle).total_seconds()
    if age <= limit:
        return None
    return Alert(
        "stalled",
        "Bot has stopped completing cycles",
        f"Last completed cycle was {age / 60:.0f} min ago "
        f"({last_cycle:%Y-%m-%d %H:%M} UTC); expected one every "
        f"{cfg.bot_interval_seconds}s. The process may be alive but wedged.",
    )


def check_churn(orders: list[dict[str, Any]], cfg: Settings, *, now: datetime) -> Alert | None:
    """
    Catch a market being bought back after it was sold.

    Buy-then-sell is ordinary trading — that is just a position that hit its
    take-profit. The loop that drained the account was the reverse: sell, then
    re-buy the same market minutes later, paying the spread each lap. The
    rotation cooldown is supposed to make this impossible, so anything this
    finds means that invariant broke.
    """
    window_h = float(cfg.rotation_cooldown_hours)
    cutoff = now - timedelta(hours=window_h)
    by_title: dict[str, list[dict[str, Any]]] = {}
    for o in orders:
        if o["ts"] >= cutoff:
            by_title.setdefault(o["title"], []).append(o)

    offenders: list[str] = []
    for title, evs in by_title.items():
        evs.sort(key=lambda e: e["ts"])
        first_sell = next((e["ts"] for e in evs if e["action"] == "SELL"), None)
        if first_sell is None:
            continue
        rebuy = next((e for e in evs if e["action"] == "BUY" and e["ts"] > first_sell), None)
        if rebuy is None:
            continue
        gap = (rebuy["ts"] - first_sell).total_seconds() / 60.0
        offenders.append(f"{title} — sold, then re-bought {gap:.0f} min later")

    if not offenders:
        return None
    return Alert(
        "churn",
        f"Re-entry detected on {len(offenders)} market(s)",
        "A market was sold and then bought again inside its "
        f"{window_h:.0f}h cooldown. This is the churn pattern that cost ~4,500 "
        "in October and should be blocked:\n\n"
        + "\n".join(f"- {o}" for o in offenders[:10]),
    )


def check_order_rate(orders: list[dict[str, Any]], cfg: Settings, *, now: datetime) -> Alert | None:
    cutoff = now - timedelta(hours=1)
    recent = [o for o in orders if o["ts"] >= cutoff]
    limit = int(cfg.guard_max_orders_per_hour)
    if len(recent) <= limit:
        return None
    notional = sum(o["qty"] * o["price"] for o in recent)
    return Alert(
        "order_rate",
        f"{len(recent)} orders in the last hour",
        f"Threshold is {limit}/hour. Turnover in that hour was "
        f"{notional:,.0f}. A runaway order rate is what produced 25M of "
        "turnover on Oct 4-5.",
    )


def check_open_orders(count: int | None, cfg: Settings) -> Alert | None:
    if count is None:
        return None
    limit = int(cfg.guard_max_open_orders)
    if count <= limit:
        return None
    return Alert(
        "open_orders",
        f"{count} resting orders have piled up",
        f"Threshold is {limit}. Unfilled limits are supposed to be cancelled "
        f"after {cfg.order_stale_seconds}s. 927 of these went unnoticed for days.",
    )


def check_drawdown(
    points: list[dict[str, Any]], cfg: Settings, *, now: datetime
) -> Alert | None:
    vals: list[tuple[datetime, float]] = []
    for row in points:
        ts = parse_ts(row.get("ts"))
        eq = _repaired_equity(row)
        if ts is not None and eq is not None:
            vals.append((ts, eq))
    if len(vals) < 2:
        return None
    vals.sort(key=lambda v: v[0])
    peak_ts, peak = max(vals, key=lambda v: v[1])
    cur_ts, cur = vals[-1]
    if peak <= 0:
        return None
    dd = (peak - cur) / peak
    if dd < float(cfg.guard_max_drawdown):
        return None
    return Alert(
        "drawdown",
        f"Equity is down {dd * 100:.1f}% from its recent peak",
        f"Peak {peak:,.0f} at {peak_ts:%Y-%m-%d %H:%M} UTC, now {cur:,.0f} at "
        f"{cur_ts:%H:%M} UTC, over a "
        f"{cfg.guard_drawdown_window_hours:.0f}h window. Alert threshold is "
        f"{cfg.guard_max_drawdown * 100:.0f}%; the circuit breaker throttles "
        f"buys at {cfg.circuit_breaker_drawdown * 100:.0f}%.",
    )


def check_rotation_budget(
    state: dict[str, Any] | None, cfg: Settings, *, now: datetime
) -> Alert | None:
    if not state:
        return None
    used = int((state.get("rotations") or {}).get(now.date().isoformat(), 0))
    limit = int(cfg.rotation_max_per_day)
    if used <= limit:
        return None
    return Alert(
        "rotation_budget",
        f"Rotation ran {used} times today, over its {limit}/day cap",
        "The daily cap is the hard ceiling on worst-case turnover. Exceeding "
        "it means the budget check is not holding.",
    )


# --- orchestration ----------------------------------------------------------


def gather_alerts(
    cfg: Settings | None = None,
    *,
    now: datetime | None = None,
    open_orders: int | None = None,
) -> list[Alert]:
    cfg = cfg or settings
    now = now or _now()
    # Cover the longest window any check needs, not a guessed byte count.
    span_h = max(48.0, float(cfg.rotation_cooldown_hours))
    cutoff = now - timedelta(hours=span_h)
    lines = tail_lines(since=cutoff)
    orders = parse_orders(lines, since=cutoff)

    rotation_state: dict[str, Any] | None = None
    if ROTATION_PATH.exists():
        try:
            rotation_state = json.loads(ROTATION_PATH.read_text())
        except Exception:  # noqa: BLE001
            log.exception("Could not read rotation state")

    points = load_equity_points(since_hours=float(cfg.guard_drawdown_window_hours))

    candidates = [
        check_stalled(last_cycle_time(lines), cfg, now=now),
        check_churn(orders, cfg, now=now),
        check_order_rate(orders, cfg, now=now),
        check_open_orders(open_orders, cfg),
        check_drawdown(points, cfg, now=now),
        check_rotation_budget(rotation_state, cfg, now=now),
    ]
    return [a for a in candidates if a is not None]


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))


def due_alerts(
    alerts: list[Alert], cfg: Settings, *, now: datetime, state: dict[str, Any]
) -> tuple[list[Alert], dict[str, Any]]:
    """Drop alerts already sent recently so a stuck condition mails once, not every cycle."""
    cooldown = float(cfg.guard_alert_cooldown_s)
    sent = dict(state.get("sent") or {})
    due: list[Alert] = []
    for a in alerts:
        last = parse_ts(sent.get(a.key))
        if last is not None and (now - last).total_seconds() < cooldown:
            continue
        due.append(a)
        sent[a.key] = now.isoformat()
    # Let a condition re-alert immediately once it has cleared and come back
    live = {a.key for a in alerts}
    sent = {k: v for k, v in sent.items() if k in live}
    state["sent"] = sent
    return due, state


def run_once(cfg: Settings | None = None, *, now: datetime | None = None) -> list[Alert]:
    """One pass: gather, de-dupe against recent sends, email what is left."""
    cfg = cfg or settings
    now = now or _now()

    open_orders: int | None = None
    try:
        from .client import SuperMarketClient

        open_orders = len(SuperMarketClient(cfg).open_orders() or [])
    except Exception:  # noqa: BLE001
        # A failed API call is not itself an anomaly worth waking anyone for;
        # the stall check will catch it if the daemon is affected too.
        log.warning("Guard could not read open orders", exc_info=True)

    alerts = gather_alerts(cfg, now=now, open_orders=open_orders)
    due, state = due_alerts(alerts, cfg, now=now, state=_load_state())
    if due:
        subject = f"[Predictions Cup] {due[0].title}" + (
            f" (+{len(due) - 1} more)" if len(due) > 1 else ""
        )
        body = "\n\n".join(f"{a.title}\n{'-' * len(a.title)}\n{a.detail}" for a in due)
        send_email(subject, body + "\n\nLogs: data/bots/runner.log\n", cfg=cfg, force=True)
    _save_state(state)
    for a in alerts:
        log.warning("ANOMALY %s: %s — %s", a.key, a.title, a.detail.splitlines()[0])
    if not alerts:
        log.info("Guard pass clean")
    return due


def watch(cfg: Settings | None = None) -> None:
    cfg = cfg or settings
    # Own process, so it needs its own handler — without this the log file
    # stays empty and there is no way to tell a live guard from a dead one.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    interval = max(30, int(cfg.guard_interval_seconds))
    log.info("Guard watching every %ss", interval)
    while True:
        try:
            run_once(cfg)
        except Exception:  # noqa: BLE001
            log.exception("Guard pass failed")
        time.sleep(interval)
