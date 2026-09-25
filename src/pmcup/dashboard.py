from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bots.runner import pid_path, status_path
from .client import SuperMarketClient
from .config import settings


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
        # also detect nohup process via status freshness? keep simple
        return False
    try:
        import os

        os.kill(int(path.read_text().strip()), 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def collect_snapshot() -> dict[str, Any]:
    """Live API + local bot artifacts."""
    slug = settings.tournament_slug or "midterm-elections"
    snap: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "dry_run": settings.dry_run,
        "live_trading": settings.can_trade_live,
        "bot_running_pidfile": _bot_running(),
        "bot_status": _read_json(status_path()) or {},
        "recent_decisions": _tail_jsonl(Path("data/bots/decisions.jsonl"), 50),
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
    return snap


def _fmt_money(v: Any) -> str:
    try:
        return f"{float(v):,.0f}"
    except Exception:  # noqa: BLE001
        return "—"


def render_html(data: dict[str, Any]) -> str:
    acct = data.get("account") or {}
    summary = data.get("position_summary") or {}
    board = data.get("leaderboard") or {}
    status = data.get("bot_status") or {}
    mode = "LIVE" if data.get("live_trading") else "PAPER/DRY"
    running = "YES" if data.get("bot_running_pidfile") else "unknown / no pidfile"

    rows_pos = []
    for p in data.get("positions") or []:
        rows_pos.append(
            "<tr>"
            f"<td>{_esc(str(p.get('marketTitle') or '')[:56])}</td>"
            f"<td>{_esc(p.get('option'))}</td>"
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
            f"<td>{_esc(str(d.get('action') or '').upper())} {_esc(str(d.get('side') or '').upper())}</td>"
            f"<td>{_esc(str(d.get('title') or '')[:48])}</td>"
            f"<td class='num'>{_esc(d.get('qty'))}</td>"
            f"<td class='num'>{_esc(d.get('price'))}</td>"
            "</tr>"
        )
    if not rows_dec:
        rows_dec.append("<tr><td colspan='7' class='muted'>No bot decisions logged yet</td></tr>")

    top = status.get("top") or []
    top_html = "".join(
        f"<li><strong>{_esc(t.get('bot'))}</strong> "
        f"{_esc(str(t.get('action') or '').upper())} {_esc(str(t.get('side') or '').upper())} "
        f"— {_esc(str(t.get('title') or '')[:60])} "
        f"<span class='muted'>qty {_esc(t.get('qty'))}</span></li>"
        for t in top[:8]
    ) or "<li class='muted'>No proposals in last status</li>"

    err = data.get("error")
    err_html = f"<div class='banner bad'>API error: {_esc(err)}</div>" if err else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Predictions Cup Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --bg: #f3efe6;
      --ink: #1c1a16;
      --muted: #6b6458;
      --line: #d9d0c0;
      --panel: #fffdf8;
      --good: #1f6b45;
      --warn: #8a5a00;
      --bad: #9b1c1c;
      --accent: #0b4f6c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 500px at 10% -10%, #e7f1f5 0%, transparent 55%),
        radial-gradient(900px 400px at 100% 0%, #f7e7d4 0%, transparent 50%),
        var(--bg);
    }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 28px 18px 60px; }}
    header {{
      display: flex; justify-content: space-between; gap: 16px; align-items: end;
      margin-bottom: 22px; flex-wrap: wrap;
    }}
    h1 {{
      font-size: 1.65rem; margin: 0 0 4px; letter-spacing: -0.02em;
    }}
    .sub {{ color: var(--muted); font-size: 0.95rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: repeat(2, 1fr); }} }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px 14px 12px;
    }}
    .stat .label {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }}
    .stat .value {{ font-family: "IBM Plex Mono", monospace; font-size: 1.35rem; margin-top: 6px; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 14px;
    }}
    h2 {{ font-size: 1.05rem; margin: 0 0 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
    th, td {{ padding: 8px 6px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 500; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }}
    .num {{ font-family: "IBM Plex Mono", monospace; text-align: right; }}
    .muted {{ color: var(--muted); }}
    .tag {{
      display: inline-block; padding: 2px 7px; border-radius: 999px;
      font-size: 0.72rem; font-family: "IBM Plex Mono", monospace;
      border: 1px solid var(--line);
    }}
    .tag.ok {{ background: #e7f6ec; color: var(--good); border-color: #b9e0c6; }}
    .tag.fail {{ background: #fdecec; color: var(--bad); border-color: #f0c2c2; }}
    .banner {{
      padding: 10px 12px; border-radius: 8px; margin-bottom: 14px;
      border: 1px solid var(--line);
    }}
    .banner.bad {{ background: #fdecec; color: var(--bad); }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ margin: 6px 0; }}
    footer {{ color: var(--muted); font-size: 0.85rem; margin-top: 8px; }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Predictions Cup</h1>
      <div class="sub">Tournament <strong>{_esc(data.get('slug'))}</strong> · auto-refresh 30s</div>
    </div>
    <div class="sub">Updated {_esc(str(data.get('generated_at') or '')[:19])}Z</div>
  </header>

  {err_html}

  <div class="grid">
    <div class="stat"><div class="label">Mode</div><div class="value">{mode}</div></div>
    <div class="stat"><div class="label">Cash / Balance</div><div class="value">{_fmt_money(acct.get('balance'))}</div></div>
    <div class="stat"><div class="label">Unrealized PnL</div><div class="value">{_fmt_money(summary.get('totalUnrealizedPnl'))}</div></div>
    <div class="stat"><div class="label">Your Rank</div><div class="value">{_esc(board.get('myRank') if board.get('myRank') is not None else '—')}</div></div>
  </div>

  <div class="grid">
    <div class="stat"><div class="label">Bot pidfile</div><div class="value" style="font-size:1rem">{running}</div></div>
    <div class="stat"><div class="label">Last cycle</div><div class="value">{_esc(status.get('cycle', '—'))}</div></div>
    <div class="stat"><div class="label">Proposals</div><div class="value">{_esc(status.get('proposals', '—'))}</div></div>
    <div class="stat"><div class="label">Executed</div><div class="value">{_esc(status.get('executed', '—'))}</div></div>
  </div>

  <section>
    <h2>Last bot proposals</h2>
    <ul>{top_html}</ul>
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
    <h2>Recent bot decisions</h2>
    <table>
      <thead><tr><th>Time</th><th>Mode</th><th>Bot</th><th>Action</th><th>Market</th><th class="num">Qty</th><th class="num">Price</th></tr></thead>
      <tbody>{''.join(rows_dec)}</tbody>
    </table>
  </section>

  <footer>
    User: {_esc(acct.get('username') or acct.get('email') or acct.get('id'))}
    · Market value {_fmt_money(summary.get('totalMarketValue'))}
    · Cost basis {_fmt_money(summary.get('totalCostBasis'))}
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
