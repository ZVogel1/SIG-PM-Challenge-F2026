from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bots.runner import pid_path, status_path
from .client import SuperMarketClient
from .config import settings
from .equity import equity_series, load_equity_points, record_equity_point, render_equity_svg
from .paper_scoreboard import summarize_paper
from .trading_window import window_status


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None


def _tail_jsonl(path: Path, n: int = 40) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    out.reverse()
    return out


def _bot_running() -> bool:
    path = pid_path()
    if not path.exists():
        return False
    try:
        import os

        os.kill(int(path.read_text().strip()), 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _status_age_seconds(status: dict[str, Any]) -> float | None:
    ts = status.get("updated_at")
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())
    except Exception:  # noqa: BLE001
        return None


def collect_snapshot() -> dict[str, Any]:
    """Live API + local bot artifacts."""
    slug = settings.tournament_slug or "midterm-elections"
    status = _read_json(status_path()) or {}
    age = _status_age_seconds(status)
    stale = age is not None and age > float(settings.bot_status_stale_seconds)
    snap: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "dry_run": settings.dry_run,
        "live_trading": settings.can_trade_live,
        "latches_live": settings.latches_allow_live,
        "trading_window": window_status(),
        "bot_running_pidfile": _bot_running(),
        "bot_status": status,
        "bot_status_age_s": age,
        "bot_status_stale": stale,
        "recent_decisions": _tail_jsonl(Path("data/bots/decisions.jsonl"), 50),
        "paper": summarize_paper(),
        "account": {},
        "positions": [],
        "position_summary": {},
        "leaderboard": {},
        "error": None,
    }
    try:
        with SuperMarketClient() as client:
            snap["account"] = client.account()
            try:
                pos = client.positions(slug)
            except Exception:  # noqa: BLE001
                pos = client.positions()
            snap["positions"] = pos.get("positions") or []
            snap["position_summary"] = pos.get("summary") or {}
            try:
                snap["leaderboard"] = client.leaderboard(slug, period="all", limit=15)
            except Exception as exc:  # noqa: BLE001
                snap["leaderboard"] = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        snap["error"] = str(exc)

    # Record / load equity curve for the chart
    try:
        summary = snap.get("position_summary") or {}
        acct = snap.get("account") or {}
        board = snap.get("leaderboard") or {}
        paper = snap.get("paper") or {}
        cash = acct.get("balance")
        if cash is None:
            cash = acct.get("availableBalance")
        record_equity_point(
            cash=float(cash) if cash is not None else None,
            positions_mv=(
                float(summary["totalMarketValue"])
                if summary.get("totalMarketValue") is not None
                else None
            ),
            upnl=(
                float(summary["totalUnrealizedPnl"])
                if summary.get("totalUnrealizedPnl") is not None
                else None
            ),
            rank=board.get("myRank"),
            paper_pnl=paper.get("total_pnl"),
            live=bool(snap.get("live_trading")),
        )
    except Exception:  # noqa: BLE001
        pass

    series = equity_series(load_equity_points())
    snap["equity"] = {
        "first": series.get("first"),
        "last": series.get("last"),
        "change": series.get("change"),
        "change_pct": series.get("change_pct"),
        "high": series.get("high"),
        "low": series.get("low"),
        "count": series.get("count"),
        "svg": render_equity_svg(series),
    }
    return snap


def _fmt_money(v: Any) -> str:
    try:
        return f"{float(v):,.0f}"
    except Exception:  # noqa: BLE001
        return "—"


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def _side_badge(action: Any, side: Any) -> str:
    """Color-coded BUY/SELL × YES/NO chip."""
    a = str(action or "").strip().lower()
    s = str(side or "").strip().lower()
    if s in {"yes", "y"}:
        tone = "yes"
    elif s in {"no", "n"}:
        tone = "no"
    else:
        tone = "unk"
    verb = a.upper() if a else "—"
    noun = s.upper() if s else "—"
    sell = " sell" if a == "sell" else ""
    return f'<span class="side {tone}{sell}">{_esc(verb)} {_esc(noun)}</span>'


def _option_badge(option: Any) -> str:
    s = str(option or "").strip().lower()
    if s in {"yes", "y"}:
        return f'<span class="side yes">{_esc(str(option))}</span>'
    if s in {"no", "n"}:
        return f'<span class="side no">{_esc(str(option))}</span>'
    return f'<span class="side unk">{_esc(option)}</span>'


def render_html(data: dict[str, Any]) -> str:
    acct = data.get("account") or {}
    summary = data.get("position_summary") or {}
    board = data.get("leaderboard") or {}
    status = data.get("bot_status") or {}
    paper = data.get("paper") or {}
    window = data.get("trading_window") or {}
    equity = data.get("equity") or {}
    mode = "LIVE" if data.get("live_trading") else "PAPER/DRY"
    mode_cls = "live" if data.get("live_trading") else "paper"
    stale = bool(data.get("bot_status_stale"))
    health = "STALE" if stale else ("OK" if data.get("bot_running_pidfile") else "CHECK")
    health_cls = "bad" if stale else ("good" if data.get("bot_running_pidfile") else "warn")
    rank = board.get("myRank")
    rank_s = "—" if rank is None else str(rank)

    eq_change = float(equity.get("change") or 0)
    eq_pct = float(equity.get("change_pct") or 0)
    eq_tone = "up" if eq_change >= 0 else "down"
    eq_change_s = f"{eq_change:+,.0f} ({eq_pct:+.2f}%)" if equity.get("count") else "—"
    eq_svg = equity.get("svg") or ""

    rows_pos = []
    for p in data.get("positions") or []:
        rows_pos.append(
            "<tr>"
            f"<td>{_esc(str(p.get('marketTitle') or '')[:56])}</td>"
            f"<td>{_option_badge(p.get('option'))}</td>"
            f"<td class='num'>{_esc(p.get('quantity'))}</td>"
            f"<td class='num'>{_esc(p.get('avgCost'))}</td>"
            f"<td class='num'>{_esc(p.get('currentPrice'))}</td>"
            f"<td class='num'>{_esc(p.get('unrealizedPnl'))}</td>"
            "</tr>"
        )
    if not rows_pos:
        rows_pos.append("<tr><td colspan='6' class='muted'>No open positions</td></tr>")

    rows_lb = []
    for i, row in enumerate(board.get("leaderboard") or [], 1):
        rows_lb.append(
            "<tr>"
            f"<td class='num'>{_esc(row.get('rank') or i)}</td>"
            f"<td>{_esc(row.get('username') or row.get('displayName') or row.get('profileId'))}</td>"
            f"<td class='num'>{_esc(row.get('portfolioValue') or row.get('pnl') or row.get('balance'))}</td>"
            "</tr>"
        )
    if not rows_lb:
        rows_lb.append("<tr><td colspan='3' class='muted'>Leaderboard unavailable</td></tr>")

    rows_dec = []
    for d in data.get("recent_decisions") or []:
        ok = "ok" if d.get("ok") else "fail"
        live = "LIVE" if d.get("live") else "dry"
        rows_dec.append(
            "<tr>"
            f"<td>{_esc(str(d.get('ts') or '')[:19])}</td>"
            f"<td><span class='tag {ok}'>{live}</span></td>"
            f"<td>{_esc(d.get('bot'))}</td>"
            f"<td>{_side_badge(d.get('action'), d.get('side'))}</td>"
            f"<td>{_esc(str(d.get('title') or '')[:48])}</td>"
            f"<td class='num'>{_esc(d.get('qty'))}</td>"
            f"<td class='num'>{_esc(d.get('price'))}</td>"
            "</tr>"
        )
    if not rows_dec:
        rows_dec.append("<tr><td colspan='7' class='muted'>No bot decisions logged yet</td></tr>")

    missing = status.get("missing_forecasts") or []
    miss_html = "".join(
        f"<li>{_esc(m.get('title') or m.get('exchange_id'))}</li>" for m in missing[:15]
    ) or "<li class='muted'>All open quotes have a fair_probs row</li>"

    top = status.get("top") or []
    top_html = "".join(
        f"<li class='prop'>"
        f"<span class='bot'>{_esc(t.get('bot'))}</span> "
        f"{_side_badge(t.get('action'), t.get('side'))} "
        f"<span class='title'>{_esc(str(t.get('title') or '')[:58])}</span> "
        f"<span class='muted qty'>×{_esc(t.get('qty'))}</span></li>"
        for t in top[:8]
    ) or "<li class='muted'>No proposals in last status</li>"

    banners = []
    if data.get("error"):
        banners.append(f"<div class='banner bad'>API error: {_esc(data.get('error'))}</div>")
    if stale:
        banners.append(
            f"<div class='banner warn'>Bot status is stale "
            f"({_esc(_fmt_age(data.get('bot_status_age_s')))} old). "
            "Check the VM runner.</div>"
        )
    if status.get("error") or status.get("error_code"):
        banners.append(
            f"<div class='banner bad'>Last cycle error: "
            f"{_esc(status.get('error') or status.get('error_code'))} "
            f"(fail streak {_esc(status.get('fail_streak', 0))})</div>"
        )
    if data.get("latches_live") and not data.get("live_trading"):
        banners.append(
            "<div class='banner warn'>Live latches are ON but outside the official "
            "trading window — orders stay paper.</div>"
        )
    err_html = "".join(banners)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Predictions Cup Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --bg: #e8eef1;
      --ink: #14212b;
      --muted: #5c6b78;
      --line: #c5d0d8;
      --panel: #ffffff;
      --good: #1b6b43;
      --warn: #9a6200;
      --bad: #a11f1f;
      --yes: #0d6e5a;
      --yes-bg: #e4f5ef;
      --yes-line: #9fd4c2;
      --no: #9a3412;
      --no-bg: #ffedd5;
      --no-line: #fdba74;
      --hero: #0c4a6e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(900px 380px at 8% -5%, #cfe6ef 0%, transparent 55%),
        radial-gradient(700px 320px at 100% 0%, #dce5ec 0%, transparent 50%),
        var(--bg);
    }}
    main {{ max-width: 1080px; margin: 0 auto; padding: 26px 16px 56px; }}
    header {{
      display: flex; justify-content: space-between; gap: 14px; align-items: end;
      margin-bottom: 18px; flex-wrap: wrap;
    }}
    h1 {{
      font-family: Fraunces, Georgia, serif;
      font-size: clamp(1.7rem, 3vw, 2.15rem);
      margin: 0 0 4px; letter-spacing: -0.03em; color: var(--hero);
    }}
    .sub {{ color: var(--muted); font-size: 0.92rem; }}
    .hero {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }}
    .mini {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 16px;
    }}
    @media (max-width: 900px) {{
      .hero {{ grid-template-columns: repeat(2, 1fr); }}
      .mini {{ grid-template-columns: repeat(3, 1fr); }}
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px 16px 14px;
    }}
    .stat.hero-stat {{
      padding: 18px 16px 16px;
      box-shadow: 0 1px 0 rgba(20, 33, 43, 0.04);
    }}
    .stat.mini-stat {{
      padding: 10px 10px 9px;
      border-radius: 9px;
      background: #f7fafb;
    }}
    .stat .label {{
      color: var(--muted); font-size: 0.72rem; text-transform: uppercase;
      letter-spacing: 0.05em; font-weight: 500;
    }}
    .stat.mini-stat .label {{ font-size: 0.65rem; letter-spacing: 0.04em; }}
    .stat .value {{
      font-family: "IBM Plex Mono", monospace;
      font-size: 1.85rem; margin-top: 8px; font-weight: 500;
      letter-spacing: -0.02em; line-height: 1.1;
    }}
    .stat.mini-stat .value {{
      font-size: 0.98rem; margin-top: 4px; font-weight: 500;
    }}
    .stat .value.mode.live {{ color: var(--good); }}
    .stat .value.mode.paper {{ color: var(--warn); }}
    .stat .value.health.good {{ color: var(--good); }}
    .stat .value.health.warn {{ color: var(--warn); }}
    .stat .value.health.bad {{ color: var(--bad); }}
    .chart-wrap {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px 14px 10px;
      margin-bottom: 12px;
    }}
    .chart-head {{
      display: flex; justify-content: space-between; gap: 12px;
      align-items: baseline; flex-wrap: wrap; margin-bottom: 8px;
    }}
    .chart-head .title {{
      font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em;
      color: var(--muted); font-weight: 600;
    }}
    .chart-head .meta {{
      font-family: "IBM Plex Mono", monospace; font-size: 0.92rem;
    }}
    .chart-head .meta.up {{ color: var(--good); }}
    .chart-head .meta.down {{ color: var(--bad); }}
    .chart-wrap svg {{ display: block; border-radius: 10px; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 12px;
    }}
    h2 {{
      font-size: 0.78rem; margin: 0 0 12px; text-transform: uppercase;
      letter-spacing: 0.06em; color: var(--muted); font-weight: 600;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    th, td {{ padding: 8px 6px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }}
    th {{ color: var(--muted); font-weight: 500; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em; }}
    .num {{ font-family: "IBM Plex Mono", monospace; text-align: right; }}
    .muted {{ color: var(--muted); }}
    .tag {{
      display: inline-block; padding: 2px 7px; border-radius: 6px;
      font-size: 0.7rem; font-family: "IBM Plex Mono", monospace;
      border: 1px solid var(--line);
    }}
    .tag.ok {{ background: #e7f6ec; color: var(--good); border-color: #b9e0c6; }}
    .tag.fail {{ background: #fdecec; color: var(--bad); border-color: #f0c2c2; }}
    .side {{
      display: inline-block; padding: 3px 8px; border-radius: 6px;
      font-size: 0.72rem; font-family: "IBM Plex Mono", monospace;
      font-weight: 500; border: 1px solid transparent; white-space: nowrap;
    }}
    .side.yes {{ background: var(--yes-bg); color: var(--yes); border-color: var(--yes-line); }}
    .side.no {{ background: var(--no-bg); color: var(--no); border-color: var(--no-line); }}
    .side.unk {{ background: #eef2f5; color: var(--muted); border-color: var(--line); }}
    .side.sell {{ opacity: 0.85; box-shadow: inset 0 0 0 1px rgba(0,0,0,0.06); }}
    .banner {{
      padding: 10px 12px; border-radius: 8px; margin-bottom: 12px;
      border: 1px solid var(--line);
    }}
    .banner.bad {{ background: #fdecec; color: var(--bad); }}
    .banner.warn {{ background: #fff6e5; color: var(--warn); }}
    ul.props, ul.miss {{ margin: 0; padding: 0; list-style: none; }}
    li.prop {{
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
      padding: 9px 0; border-bottom: 1px solid var(--line);
    }}
    li.prop:last-child {{ border-bottom: 0; }}
    li.prop .bot {{
      font-family: "IBM Plex Mono", monospace; font-size: 0.72rem;
      color: var(--muted); min-width: 7.5rem;
    }}
    li.prop .title {{ flex: 1; min-width: 12rem; }}
    li.prop .qty {{ font-family: "IBM Plex Mono", monospace; font-size: 0.85rem; }}
    ul.miss li {{ margin: 6px 0; font-size: 0.9rem; }}
    footer {{ color: var(--muted); font-size: 0.82rem; margin-top: 6px; }}
    .legend {{
      display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
      margin: -4px 0 12px; font-size: 0.8rem; color: var(--muted);
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Predictions Cup</h1>
      <div class="sub">Tournament <strong>{_esc(data.get('slug'))}</strong> · window
        <strong>{_esc(window.get('phase'))}</strong> · auto-refresh 30s</div>
    </div>
    <div class="sub">Updated {_esc(str(data.get('generated_at') or '')[:19])}Z</div>
  </header>

  {err_html}

  <div class="hero">
    <div class="stat hero-stat">
      <div class="label">Your rank</div>
      <div class="value">{_esc(rank_s)}</div>
    </div>
    <div class="stat hero-stat">
      <div class="label">Cash / balance</div>
      <div class="value">{_fmt_money(acct.get('balance'))}</div>
    </div>
    <div class="stat hero-stat">
      <div class="label">Unrealized PnL</div>
      <div class="value">{_fmt_money(summary.get('totalUnrealizedPnl'))}</div>
    </div>
    <div class="stat hero-stat">
      <div class="label">Mode</div>
      <div class="value mode {mode_cls}">{mode}</div>
    </div>
  </div>

  <div class="mini">
    <div class="stat mini-stat"><div class="label">Bot</div><div class="value health {health_cls}">{health}</div></div>
    <div class="stat mini-stat"><div class="label">Status age</div><div class="value">{_esc(_fmt_age(data.get('bot_status_age_s')))}</div></div>
    <div class="stat mini-stat"><div class="label">Cycle</div><div class="value">{_esc(status.get('cycle', '—'))}</div></div>
    <div class="stat mini-stat"><div class="label">Exec / ok</div><div class="value">{_esc(status.get('executed', '—'))}/{_esc(status.get('ok', '—'))}</div></div>
    <div class="stat mini-stat"><div class="label">Paper PnL</div><div class="value">{_fmt_money(paper.get('total_pnl'))}</div></div>
    <div class="stat mini-stat"><div class="label">Fail streak</div><div class="value">{_esc(status.get('fail_streak', 0))}</div></div>
  </div>

  <div class="chart-wrap">
    <div class="chart-head">
      <div class="title">Portfolio equity</div>
      <div class="meta {eq_tone}">
        {_fmt_money(equity.get('last'))} · {_esc(eq_change_s)}
        · {_esc(equity.get('count') or 0)} pts
      </div>
    </div>
    {eq_svg}
  </div>

  <section>
    <h2>Last bot proposals</h2>
    <div class="legend">
      <span class="side yes">BUY YES</span>
      <span class="side no">BUY NO</span>
      <span class="muted">Sell chips use the same colors, slightly muted</span>
    </div>
    <ul class="props">{top_html}</ul>
  </section>

  <section>
    <h2>Recent bot decisions</h2>
    <table>
      <thead><tr><th>Time</th><th>Mode</th><th>Bot</th><th>Action</th><th>Market</th><th class="num">Qty</th><th class="num">Price</th></tr></thead>
      <tbody>{''.join(rows_dec)}</tbody>
    </table>
  </section>

  <section>
    <h2>Open positions</h2>
    <table>
      <thead><tr><th>Market</th><th>Side</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">Mark</th><th class="num">uPnL</th></tr></thead>
      <tbody>{''.join(rows_pos)}</tbody>
    </table>
  </section>

  <section>
    <h2>Leaderboard</h2>
    <table>
      <thead><tr><th class="num">Rank</th><th>User</th><th class="num">Value</th></tr></thead>
      <tbody>{''.join(rows_lb)}</tbody>
    </table>
  </section>

  <section>
    <h2>Markets missing fair probs</h2>
    <ul class="miss">{miss_html}</ul>
  </section>

  <footer>
    User: {_esc(acct.get('username') or acct.get('email') or acct.get('id'))}
    · Market value {_fmt_money(summary.get('totalMarketValue'))}
    · Cost basis {_fmt_money(summary.get('totalCostBasis'))}
    · Missing forecasts {_esc(status.get('missing_forecast_count', len(missing)))}
    · Tunnel-only (127.0.0.1)
  </footer>
</main>
</body>
</html>
"""


def _esc(v: Any) -> str:
    s = "" if v is None else str(v)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/api", "/api/stats", "/stats.json"}:
            payload = collect_snapshot()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in {"/", "/index.html", "/dashboard"}:
            html = render_html(collect_snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        self.send_response(404)
        self.end_headers()


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"Dashboard: http://{host}:{port}")
    print(f"JSON:       http://{host}:{port}/api/stats")
    print("Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        httpd.server_close()
