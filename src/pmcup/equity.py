from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def equity_path() -> Path:
    path = Path("data/bots")
    path.mkdir(parents=True, exist_ok=True)
    return path / "equity_curve.jsonl"


def record_equity_point(
    *,
    cash: float | None = None,
    positions_mv: float | None = None,
    upnl: float | None = None,
    rank: int | None = None,
    paper_pnl: float | None = None,
    live: bool | None = None,
    min_interval_s: float = 45.0,
) -> dict[str, Any]:
    """
    Append one equity snapshot.
    equity ≈ cash + open position mark (falls back to whichever is available).
    """
    cash_f = float(cash) if cash is not None else None
    mv_f = float(positions_mv) if positions_mv is not None else None
    paper_f = float(paper_pnl) if paper_pnl is not None else None

    # Real marked equity always wins. The paper fallback is only for the
    # pre-open case where the account has no positions to mark.
    if cash_f is not None and mv_f is not None:
        equity = cash_f + mv_f
    elif cash_f is not None:
        equity = cash_f + (paper_f or 0.0)
    elif mv_f is not None:
        equity = mv_f
    elif paper_f is not None:
        equity = 100_000.0 + paper_f
    else:
        equity = None

    now = datetime.now(timezone.utc)
    point = {
        "ts": now.isoformat(),
        "equity": equity,
        "cash": cash_f,
        "positions_mv": mv_f,
        "upnl": float(upnl) if upnl is not None else None,
        "rank": rank,
        "paper_pnl": float(paper_pnl) if paper_pnl is not None else None,
        "live": live,
    }
    # The dashboard records on every page render; throttle so refreshing the
    # page doesn't bunch the curve up with duplicate samples.
    if min_interval_s > 0 and _seconds_since_last(now) < min_interval_s:
        return point
    with equity_path().open("a") as f:
        f.write(json.dumps(point) + "\n")
    return point


def _seconds_since_last(now: datetime) -> float:
    path = equity_path()
    if not path.exists():
        return float("inf")
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(tail):
            if not line.strip():
                continue
            ts = json.loads(line).get("ts")
            if ts:
                prev = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                return (now - prev).total_seconds()
    except Exception:  # noqa: BLE001
        return float("inf")
    return float("inf")


def load_equity_points(
    limit: int = 20000,
    *,
    since_hours: float | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    path = equity_path()
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    cutoff = since
    if cutoff is None and since_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(since_hours))
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("equity") is None and row.get("paper_pnl") is None:
            continue
        if cutoff is not None:
            ts = parse_ts(row.get("ts"))
            if ts is None or ts < cutoff:
                continue
        out.append(row)
    return out


def parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _repaired_equity(row: dict[str, Any]) -> float | None:
    """
    Equity for one stored row, recomputed from its parts.

    Older rows were written with a formula that dropped position value whenever
    the `live` flag read false, so trust cash + marked positions when present.
    """
    cash = row.get("cash")
    mv = row.get("positions_mv")
    if cash is not None and mv is not None:
        return float(cash) + float(mv)
    # Cash with no position mark means the positions call failed that cycle;
    # plotting it would show bare cash as if it were the whole portfolio.
    if cash is not None:
        return None
    if row.get("paper_pnl") is not None:
        return 100_000.0 + float(row["paper_pnl"])
    if row.get("equity") is not None:
        return float(row["equity"])
    return None


def downsample(
    xs: list[datetime], ys: list[float], max_points: int = 400
) -> tuple[list[datetime], list[float]]:
    """Thin a long series while keeping its shape, highs and lows."""
    n = len(ys)
    if n <= max_points or max_points < 4:
        return xs, ys
    bucket = n / float(max_points)
    out_x: list[datetime] = []
    out_y: list[float] = []
    for i in range(max_points):
        start = int(i * bucket)
        end = max(start + 1, int((i + 1) * bucket))
        chunk = ys[start:end]
        # Keep the extreme of each bucket so spikes survive thinning
        lo_i = start + chunk.index(min(chunk))
        hi_i = start + chunk.index(max(chunk))
        for idx in sorted({lo_i, hi_i}):
            out_x.append(xs[idx])
            out_y.append(ys[idx])
    if out_x[-1] != xs[-1]:
        out_x.append(xs[-1])
        out_y.append(ys[-1])
    return out_x, out_y


def equity_series(points: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build chart-ready series + summary stats."""
    pts = points if points is not None else load_equity_points()
    ts_list: list[datetime] = []
    ys: list[float] = []
    for p in pts:
        value = _repaired_equity(p)
        ts = parse_ts(p.get("ts"))
        if value is None or ts is None:
            continue
        ts_list.append(ts)
        ys.append(value)

    base = {
        "points": pts,
        "ts": ts_list,
        "xs": [t.isoformat() for t in ts_list],
        "ys": ys,
        "count": len(ys),
    }
    if len(ys) < 2:
        base.update(
            {
                "first": ys[0] if ys else None,
                "last": ys[-1] if ys else None,
                "change": 0.0,
                "change_pct": 0.0,
                "high": max(ys) if ys else None,
                "low": min(ys) if ys else None,
            }
        )
        return base

    first, last = ys[0], ys[-1]
    change = last - first
    base.update(
        {
            "first": first,
            "last": last,
            "change": change,
            "change_pct": (change / first * 100.0) if first else 0.0,
            "high": max(ys),
            "low": min(ys),
        }
    )
    return base



def _nice_step(span: float, target_ticks: int = 4) -> float:
    """Round gridline spacing to a 1/2/5 x 10^n value so labels read cleanly."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target_ticks)
    magnitude = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        if raw <= magnitude * mult:
            return magnitude * mult
    return magnitude * 10


def _et(ts: datetime) -> datetime:
    try:
        return ts.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        return ts


def _time_labels(ts_list: list[datetime], count: int = 5) -> list[tuple[datetime, str]]:
    """Evenly spaced time ticks, formatted by how much ground the chart covers."""
    if not ts_list:
        return []
    start, end = ts_list[0], ts_list[-1]
    span_h = (end - start).total_seconds() / 3600.0
    fmt = "%H:%M" if span_h <= 24 else "%b %-d"
    out: list[tuple[datetime, str]] = []
    for i in range(count):
        frac = i / float(count - 1) if count > 1 else 0.0
        at = start + (end - start) * frac
        out.append((at, _et(at).strftime(fmt)))
    return out


def render_equity_svg(
    series: dict[str, Any],
    *,
    width: int = 900,
    height: int = 280,
    baseline: float = 100_000.0,
) -> str:
    """Pure-SVG equity curve on a real time axis (no JS dependency)."""
    ys: list[float] = list(series.get("ys") or [])
    ts_list: list[datetime] = list(series.get("ts") or [])
    if len(ys) < 2 or len(ts_list) != len(ys):
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" '
            f'role="img" aria-label="Equity chart">'
            f'<rect width="100%" height="100%" fill="#f7fafb" rx="10"/>'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'fill="#5c6b78" font-family="IBM Plex Sans, sans-serif" font-size="14">'
            f"Collecting history — curve fills in as cycles run"
            f"</text></svg>"
        )

    ts_list, ys = downsample(ts_list, ys)
    pad_l, pad_r, pad_t, pad_b = 64, 64, 22, 34
    w = width - pad_l - pad_r
    h = height - pad_t - pad_b

    lo_v, hi_v = min(ys), max(ys)
    if baseline and lo_v <= baseline <= hi_v:
        pass
    span = max(hi_v - lo_v, 1.0)
    step = _nice_step(span * 1.2)
    lo = math.floor((lo_v - span * 0.08) / step) * step
    hi = math.ceil((hi_v + span * 0.08) / step) * step
    y_span = max(hi - lo, 1.0)

    t0 = ts_list[0].timestamp()
    t_span = max(ts_list[-1].timestamp() - t0, 1.0)

    def px(ts: datetime) -> float:
        return pad_l + ((ts.timestamp() - t0) / t_span) * w

    def py(v: float) -> float:
        return pad_t + (1.0 - (v - lo) / y_span) * h

    coords = [(px(t), py(v)) for t, v in zip(ts_list, ys)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"{coords[0][0]:.1f},{pad_t + h:.1f} {line} {coords[-1][0]:.1f},{pad_t + h:.1f}"

    up = float(series.get("change") or 0) >= 0
    stroke = "#1b6b43" if up else "#a11f1f"
    fill = "#d8efe4" if up else "#f8d9d9"

    # Horizontal gridlines with real values
    grid: list[str] = []
    v = lo
    while v <= hi + 1e-9:
        y = py(v)
        grid.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + w}" y2="{y:.1f}" '
            f'stroke="#e6ecf0" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#8a97a3" '
            f'font-family="IBM Plex Mono, monospace" font-size="11">{v:,.0f}</text>'
        )
        v += step

    # Starting bankroll reference
    base_line = ""
    if baseline and lo <= baseline <= hi:
        by = py(baseline)
        base_line = (
            f'<line x1="{pad_l}" y1="{by:.1f}" x2="{pad_l + w}" y2="{by:.1f}" '
            f'stroke="#9aa7b2" stroke-width="1" stroke-dasharray="4 4"/>'
            f'<text x="{pad_l + w + 6}" y="{by + 4:.1f}" fill="#8a97a3" '
            f'font-family="IBM Plex Mono, monospace" font-size="10">start</text>'
        )

    ticks = "".join(
        f'<text x="{px(at):.1f}" y="{height - 12}" text-anchor="middle" fill="#8a97a3" '
        f'font-family="IBM Plex Mono, monospace" font-size="11">{label}</text>'
        for at, label in _time_labels(ts_list)
    )

    hi_i = ys.index(max(ys))
    lo_i = ys.index(min(ys))
    marks = ""
    for idx, label, anchor_dy in ((hi_i, f"{ys[hi_i]:,.0f}", -8), (lo_i, f"{ys[lo_i]:,.0f}", 15)):
        mx, my = coords[idx]
        anchor = "start" if mx < pad_l + w * 0.5 else "end"
        marks += (
            f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="2.5" fill="#8a97a3"/>'
            f'<text x="{mx:.1f}" y="{my + anchor_dy:.1f}" text-anchor="{anchor}" '
            f'fill="#8a97a3" font-family="IBM Plex Mono, monospace" '
            f'font-size="10">{label}</text>'
        )

    last_x, last_y = coords[-1]
    return f"""<svg viewBox="0 0 {width} {height}" width="100%"
      role="img" aria-label="Portfolio equity over time">
  <defs>
    <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{fill}" stop-opacity="0.9"/>
      <stop offset="100%" stop-color="{fill}" stop-opacity="0.08"/>
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="#ffffff" rx="10"/>
  {"".join(grid)}
  {base_line}
  <polyline fill="url(#eqFill)" stroke="none" points="{area}"/>
  <polyline fill="none" stroke="{stroke}" stroke-width="2"
    stroke-linejoin="round" stroke-linecap="round" points="{line}"/>
  {marks}
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="{stroke}"/>
  <text x="{last_x - 8:.1f}" y="{last_y - 10:.1f}" text-anchor="end" fill="{stroke}"
    font-family="IBM Plex Mono, monospace" font-size="12"
    font-weight="600">{ys[-1]:,.0f}</text>
  {ticks}
</svg>"""
