from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
) -> dict[str, Any]:
    """
    Append one equity snapshot.
    equity ≈ cash + open position mark (falls back to whichever is available).
    """
    cash_f = float(cash) if cash is not None else None
    mv_f = float(positions_mv) if positions_mv is not None else None
    paper_f = float(paper_pnl) if paper_pnl is not None else None

    if live is False and paper_f is not None:
        # Pre-open / dry: show paper PnL as movement around cash/bankroll
        base = cash_f if cash_f is not None else 100_000.0
        equity = base + paper_f
    elif cash_f is not None and mv_f is not None:
        equity = cash_f + mv_f
    elif cash_f is not None:
        equity = cash_f
    elif mv_f is not None:
        equity = mv_f
    elif paper_f is not None:
        equity = 100_000.0 + paper_f
    else:
        equity = None

    point = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity": equity,
        "cash": cash_f,
        "positions_mv": mv_f,
        "upnl": float(upnl) if upnl is not None else None,
        "rank": rank,
        "paper_pnl": float(paper_pnl) if paper_pnl is not None else None,
        "live": live,
    }
    with equity_path().open("a") as f:
        f.write(json.dumps(point) + "\n")
    return point


def load_equity_points(limit: int = 400) -> list[dict[str, Any]]:
    path = equity_path()
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("equity") is None and row.get("paper_pnl") is None:
            continue
        out.append(row)
    return out


def equity_series(points: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build chart-ready series + summary stats."""
    pts = points if points is not None else load_equity_points()
    # Prefer marked equity; in pure paper with no API equity use paper ledger as overlay
    xs: list[str] = []
    ys: list[float] = []
    for p in pts:
        if p.get("equity") is not None:
            xs.append(str(p.get("ts") or ""))
            ys.append(float(p["equity"]))
        elif p.get("paper_pnl") is not None:
            # Offset paper PnL around 100k so the chart still reads like a portfolio
            xs.append(str(p.get("ts") or ""))
            ys.append(100_000.0 + float(p["paper_pnl"]))

    if len(ys) < 2:
        return {
            "points": pts,
            "xs": xs,
            "ys": ys,
            "first": ys[0] if ys else None,
            "last": ys[-1] if ys else None,
            "change": 0.0,
            "change_pct": 0.0,
            "high": max(ys) if ys else None,
            "low": min(ys) if ys else None,
            "count": len(ys),
        }

    first, last = ys[0], ys[-1]
    change = last - first
    change_pct = (change / first * 100.0) if first else 0.0
    return {
        "points": pts,
        "xs": xs,
        "ys": ys,
        "first": first,
        "last": last,
        "change": change,
        "change_pct": change_pct,
        "high": max(ys),
        "low": min(ys),
        "count": len(ys),
    }


def render_equity_svg(
    series: dict[str, Any],
    *,
    width: int = 640,
    height: int = 200,
) -> str:
    """Pure SVG equity / paper curve (no JS dependency)."""
    ys: list[float] = list(series.get("ys") or [])
    xs: list[str] = list(series.get("xs") or [])
    if len(ys) < 2:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="Equity chart">'
            f'<rect width="100%" height="100%" fill="#f7fafb" rx="10"/>'
            f'<text x="{width/2}" y="{height/2}" text-anchor="middle" '
            f'fill="#5c6b78" font-family="IBM Plex Sans, sans-serif" font-size="14">'
            f"Collecting history — curve fills in as cycles run"
            f"</text></svg>"
        )

    pad_l, pad_r, pad_t, pad_b = 8, 8, 16, 28
    w = width - pad_l - pad_r
    h = height - pad_t - pad_b
    lo = float(series["low"])
    hi = float(series["high"])
    span = max(hi - lo, 1.0)
    # pad vertical range a bit
    lo -= span * 0.08
    hi += span * 0.08
    span = hi - lo

    def xy(i: int, y: float) -> tuple[float, float]:
        x = pad_l + (i / (len(ys) - 1)) * w
        yy = pad_t + (1.0 - (y - lo) / span) * h
        return x, yy

    coords = [xy(i, y) for i, y in enumerate(ys)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = (
        f"{pad_l:.1f},{pad_t + h:.1f} "
        + line
        + f" {pad_l + w:.1f},{pad_t + h:.1f}"
    )

    up = float(series.get("change") or 0) >= 0
    stroke = "#1b6b43" if up else "#a11f1f"
    fill = "#d8efe4" if up else "#f8d9d9"
    last_x, last_y = coords[-1]
    first_label = xs[0][11:16] if len(xs[0]) >= 16 else xs[0][:16]
    last_label = xs[-1][11:16] if len(xs[-1]) >= 16 else xs[-1][:16]

    return f"""<svg viewBox="0 0 {width} {height}" width="100%" height="{height}"
      role="img" aria-label="Portfolio equity over time" preserveAspectRatio="none">
  <defs>
    <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{fill}" stop-opacity="0.95"/>
      <stop offset="100%" stop-color="{fill}" stop-opacity="0.15"/>
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="#ffffff" rx="10"/>
  <polyline fill="url(#eqFill)" stroke="none" points="{area}"/>
  <polyline fill="none" stroke="{stroke}" stroke-width="2.5"
    stroke-linejoin="round" stroke-linecap="round" points="{line}"/>
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="{stroke}"/>
  <text x="{pad_l}" y="{height - 8}" fill="#5c6b78"
    font-family="IBM Plex Mono, monospace" font-size="11">{first_label}</text>
  <text x="{width - pad_r}" y="{height - 8}" fill="#5c6b78" text-anchor="end"
    font-family="IBM Plex Mono, monospace" font-size="11">{last_label}</text>
  <text x="{width - pad_r}" y="18" fill="{stroke}" text-anchor="end"
    font-family="IBM Plex Mono, monospace" font-size="12">{hi:,.0f} high</text>
  <text x="{pad_l}" y="18" fill="#5c6b78"
    font-family="IBM Plex Mono, monospace" font-size="12">{lo:,.0f} low</text>
</svg>"""
