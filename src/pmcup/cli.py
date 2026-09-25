from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .client import SuperMarketClient
from .config import settings
from .fair_probs import bootstrap_template, default_path, ensure_example
from .forecasts import update_fair_probs_from_forecasts
from .paper import apply_ideas, mark_portfolio
from .research import edge_persistence, snapshot_scan
from .scanner import scan
from .sizing import round_tick

app = typer.Typer(
    add_completion=False,
    help="Predictions Cup helper — finds edges and sizes trades for maximizing #1 chance.",
)
bots_app = typer.Typer(help="Unattended multi-bot trading (runs while you're busy).")
app.add_typer(bots_app, name="bots")
console = Console()


@bots_app.command("test-email")
def bots_test_email() -> None:
    """Send a test notification email using SMTP settings in .env."""
    from .notify import email_configured, send_email

    if not email_configured():
        console.print(
            Panel.fit(
                "Add these to .env first:\n"
                "NOTIFY_EMAIL_TO=you@example.com\n"
                "SMTP_HOST=smtp.gmail.com\n"
                "SMTP_PORT=587\n"
                "SMTP_USER=you@gmail.com\n"
                "SMTP_PASSWORD=your_app_password\n"
                "NOTIFY_EMAIL_FROM=you@gmail.com",
                title="Email not configured",
            )
        )
        raise typer.Exit(1)
    ok = send_email(
        "[Predictions Cup] Test notification",
        "If you got this, stop alerts are working.\n",
    )
    if ok:
        console.print("[green]Test email sent.[/green]")
    else:
        console.print("[red]Send failed — check SMTP settings / runner.log[/red]")
        raise typer.Exit(1)


@bots_app.command("once")
def bots_once() -> None:
    """Run a single multi-bot decision cycle (dry-run unless LIVE_TRADING=true)."""
    from .bots.runner import run_cycle, setup_logging

    setup_logging()
    summary = run_cycle(settings, cycle=0)
    console.print(Panel.fit(str(summary), title="Bot cycle"))


@bots_app.command("run")
def bots_run(
    foreground: bool = typer.Option(
        False,
        "--foreground",
        "-f",
        help="Run in this terminal (Ctrl+C to stop). Default starts in background.",
    ),
) -> None:
    """
    Start the multi-bot loop (every BOT_INTERVAL_SECONDS).

    Bots:
      - edge_hunter: forecast vs market
      - constraint_arb: cross-market inconsistencies
      - risk_manager: sell/take-profit when edge flips or decays

    Live orders require DRY_RUN=false AND LIVE_TRADING=true in .env
    """
    import os
    import subprocess
    import sys

    from .bots.runner import pid_path, run_forever, setup_logging

    if pid_path().exists():
        old = pid_path().read_text().strip()
        console.print(f"[yellow]Runner already seems running (pid {old}).[/yellow] "
                      "Use `pmcup bots stop` first.")
        raise typer.Exit(1)

    mode = "LIVE" if settings.can_trade_live else "PAPER/DRY"
    console.print(
        Panel.fit(
            f"mode: {mode}\n"
            f"interval: {settings.bot_interval_seconds}s\n"
            f"max orders/cycle: {settings.bot_max_orders_per_cycle}\n"
            f"bots: edge={settings.bot_enable_edge} "
            f"constraint={settings.bot_enable_constraint} "
            f"risk={settings.bot_enable_risk}",
            title="Starting bots",
        )
    )

    if foreground:
        setup_logging()
        run_forever(settings)
        return

    # Background via nohup — always put src on PYTHONPATH for reliability
    log_path = Path("data/bots/runner.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    src = str(Path.cwd() / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, "-m", "pmcup.bots.daemon"]
    with log_path.open("a") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path.cwd()),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    pid_path().write_text(str(proc.pid))

    # macOS: keep machine awake while bots run (helps on power adapter;
    # closing the lid on battery still usually sleeps the Mac)
    caffeinate_note = ""
    if sys.platform == "darwin":
        try:
            subprocess.Popen(
                ["caffeinate", "-dims", "-w", str(proc.pid)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            caffeinate_note = (
                "\n[macOS] caffeinate enabled (prevents idle sleep while bots run).\n"
                "Still: keep plugged in. Closing the lid on battery will sleep the Mac.\n"
                "Best: lid open or clamshell (power + external display), or a cloud box."
            )
        except FileNotFoundError:
            caffeinate_note = "\n[macOS] caffeinate not found — install Xcode CLT or leave lid open."

    console.print(
        f"[green]Bots running in background[/green] pid={proc.pid}\n"
        f"Logs: {log_path}\n"
        f"Status: pmcup bots status\n"
        f"Stop: pmcup bots stop"
        f"{caffeinate_note}"
    )


@bots_app.command("stop")
def bots_stop() -> None:
    """Stop the background bot runner."""
    import os
    import signal

    from .bots.runner import pid_path

    path = pid_path()
    if not path.exists():
        console.print("[yellow]No runner pid file — nothing to stop.[/yellow]")
        return
    pid = int(path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent SIGTERM to pid {pid}[/green]")
    except ProcessLookupError:
        console.print(f"[yellow]Process {pid} not found — cleaning pid file.[/yellow]")
    if path.exists():
        path.unlink()


@bots_app.command("status")
def bots_status() -> None:
    """Show whether bots are running and last cycle summary."""
    import json

    from .bots.runner import pid_path, status_path

    running = False
    pid = None
    if pid_path().exists():
        pid = pid_path().read_text().strip()
        try:
            import os

            os.kill(int(pid), 0)
            running = True
        except (ProcessLookupError, ValueError, PermissionError):
            running = False
    console.print(
        Panel.fit(
            f"running: {running}\n"
            f"pid: {pid}\n"
            f"live_trading: {settings.can_trade_live}\n"
            f"interval: {settings.bot_interval_seconds}s",
            title="Bot status",
        )
    )
    if status_path().exists():
        data = json.loads(status_path().read_text())
        console.print(data)


@app.command("dashboard")
def dashboard_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address (0.0.0.0 to expose on VM)"),
    port: int = typer.Option(8080, help="Port"),
) -> None:
    """
    Open a live web dashboard: balance, rank, positions, bot decisions.

    Safe default binds to localhost. On the GCP VM use host 0.0.0.0 and an SSH tunnel,
    or open firewall port 8080.
    """
    from .dashboard import serve

    console.print(
        Panel.fit(
            f"Starting dashboard on http://{host}:{port}\n"
            "From your laptop (recommended SSH tunnel):\n"
            "  gcloud compute ssh pmcup-bots1 --zone=us-east4-b -- -L 8080:localhost:8080\n"
            "Then open http://127.0.0.1:8080",
            title="Dashboard",
        )
    )
    serve(host=host, port=port)


@app.command("setup")
def setup_cmd() -> None:
    """Create local folders and example fair-probability sheet."""
    ensure_example()
    env = Path(".env")
    if not env.exists():
        example = Path(".env.example")
        if example.exists():
            env.write_text(example.read_text())
            console.print("[green]Created .env from .env.example — paste your API key.[/green]")
        else:
            console.print("[yellow]No .env.example found.[/yellow]")
    else:
        console.print("[dim].env already exists[/dim]")
    console.print(
        Panel.fit(
            "1) Register at https://sig.thesuper.market\n"
            "2) Settings → API Keys → create key with scopes: read + trade\n"
            "3) Paste into .env as SUPERMARKET_API_KEY=...\n"
            "4) Run: pmcup whoami\n"
            "5) Run: pmcup bootstrap-probs\n"
            "6) Run: pmcup update-probs   # polls+fundamentals consensus\n"
            "7) Run: pmcup scan",
            title="First-place setup",
        )
    )


@app.command("whoami")
def whoami() -> None:
    """Verify API key and show balance / tournaments."""
    with SuperMarketClient() as client:
        acct = client.account()
        tournaments = client.tournaments()
    console.print(
        Panel.fit(
            f"user: {acct.get('username') or acct.get('email') or acct.get('id')}\n"
            f"balance: {acct.get('balance')}",
            title="Account",
        )
    )
    table = Table(title="Tournaments")
    table.add_column("slug")
    table.add_column("name")
    table.add_column("status")
    table.add_column("myBalance")
    table.add_column("currency")
    for t in tournaments:
        table.add_row(
            str(t.get("slug")),
            str(t.get("name")),
            str(t.get("status")),
            f"{t.get('myBalance')}",
            str(t.get("currencyName")),
        )
    console.print(table)
    if tournaments and not settings.tournament_slug:
        console.print(
            f"[yellow]Tip:[/yellow] set TOURNAMENT_SLUG={tournaments[0]['slug']} in .env"
        )


@app.command("tournaments")
def tournaments_cmd() -> None:
    """List accessible tournaments."""
    whoami()


@app.command("update-probs")
def update_probs_cmd(
    blend: float = typer.Option(
        0.0,
        help="Mix current sheet values in (0=pure forecast, 0.3=70% forecast/30% sheet)",
    ),
) -> None:
    """
    Set fair_yes from a middle-ground forecast model (polls + fundamentals).

    Uses VotePredictor (votepredictor.com) — not your gut, not partisan blogs.
    This is the recommended way to feed the bot.
    """
    result = update_fair_probs_from_forecasts(blend_with_market=blend)
    console.print(
        Panel.fit(
            f"Updated {result['matched']} rows in {result['path']}\n"
            f"{result['attribution']}\n"
            "Next: pmcup scan",
            title="Fair probs ← consensus forecast",
        )
    )
    if result["missing_governor_ids"]:
        console.print(
            f"[dim]No forecast for: {', '.join(result['missing_governor_ids'][:8])}[/dim]"
        )


@app.command("bootstrap-probs")
def bootstrap_probs() -> None:
    """
    Build data/fair_probs.csv from live markets.

    Leave fair_yes alone (= market) for races you have no view on.
    Only edit rows where you think the market is wrong.
    """
    with SuperMarketClient() as client:
        slug = settings.tournament_slug
        if not slug:
            ts = client.tournaments()
            if not ts:
                raise SystemExit("No tournaments found.")
            slug = ts[0]["slug"]
            console.print(f"[dim]Using tournament {slug}[/dim]")
        t = client.tournament(slug)
        tournament_id = t["id"]
        markets = client.list_tournament_markets(slug, status="open")
        if not markets:
            markets = client.list_markets(tournament_id=tournament_id, status="open")
        exchange_ids = [
            str(ex["id"])
            for m in markets
            for ex in (m.get("exchanges") or [])
            if ex.get("id") is not None
        ]
        quotes = (
            client.bulk_prices(exchange_ids, tournament_id=tournament_id)
            if exchange_ids
            else []
        )
        price_by_exchange = {str(q["exchangeId"]): q for q in quotes}
    path = bootstrap_template(markets, price_by_exchange=price_by_exchange)
    console.print(
        f"[green]Wrote {path}[/green] with {len(markets)} markets "
        f"({len(price_by_exchange)} price quotes). "
        "Edit fair_yes only where you disagree with the crowd."
    )


@app.command("scan")
def scan_cmd(
    top: int = typer.Option(15, help="How many ideas to show"),
    execute_top: int = typer.Option(
        0,
        help="If >0 and DRY_RUN=false, place the top N ideas as limit orders",
    ),
) -> None:
    """Scan for edges and print a plain-English playbook."""
    result = scan()
    console.print(
        Panel.fit(
            f"tournament: {result['slug']}\n"
            f"bankroll: {result['bankroll']:,.0f} SUSQies\n"
            f"open markets: {result['market_count']}\n"
            f"fair probs loaded: {result['fair_prob_count']}\n"
            f"constraint violations: {result['constraint_violations']}\n"
            f"dry_run: {result['dry_run']}",
            title="Scan summary",
        )
    )

    ideas = result["ideas"][:top]
    if not ideas:
        console.print(
            "[yellow]No trades cleared the edge bar.[/yellow] "
            "Edit data/fair_probs.csv or lower MIN_EDGE in .env."
        )
        return

    table = Table(title="Recommended trades (maximize #1 chance)")
    table.add_column("#", justify="right")
    table.add_column("Action")
    table.add_column("Market")
    table.add_column("Net", justify="right")
    table.add_column("Raw", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("Stake", justify="right")
    table.add_column("Source")
    for i, idea in enumerate(ideas, 1):
        table.add_row(
            str(i),
            f"{idea.action.upper()} {idea.side.upper()}",
            idea.market_title[:42],
            f"{idea.net_edge * 100:.1f}¢",
            f"{idea.edge * 100:.1f}¢",
            f"{idea.confidence:.2f}",
            f"{idea.stake:,.0f}",
            idea.source,
        )
    console.print(table)

    console.print("\n[bold]Plain English[/bold]")
    for idea in ideas:
        console.print(f"• {idea.plain_english}")

    if execute_top > 0:
        _execute(ideas[:execute_top], tournament_id=result["tournament_id"])


def _execute(ideas, *, tournament_id: str) -> None:
    with SuperMarketClient() as client:
        for idea in ideas:
            limit = round_tick(idea.market_price)
            resp = client.place_order(
                exchange_id=idea.exchange_id,
                side=idea.side,
                action=idea.action,
                quantity=idea.size,
                price=limit,
                tournament_id=tournament_id,
            )
            console.print(resp)


@app.command("daily")
def daily_cmd(
    paper_top: int = typer.Option(5, help="Paper-fill top N ideas after scan"),
    top: int = typer.Option(10, help="How many ideas to print"),
) -> None:
    """
    Pre-open automation loop:
      update forecasts → scan edges → snapshot history → paper trade top ideas
    Run this daily (or via cron) until Oct 1.
    """
    console.print("[bold]1/4 update-probs[/bold]")
    upd = update_fair_probs_from_forecasts()
    console.print(f"  updated {upd['matched']} fair probs")

    console.print("[bold]2/4 scan[/bold]")
    result = scan()
    ideas = result["ideas"][:top]
    table = Table(title="Today's edges")
    table.add_column("#", justify="right")
    table.add_column("Action")
    table.add_column("Market")
    table.add_column("Net", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("Stake", justify="right")
    for i, idea in enumerate(ideas, 1):
        table.add_row(
            str(i),
            f"{idea.action.upper()} {idea.side.upper()}",
            idea.market_title[:42],
            f"{idea.net_edge * 100:.1f}¢",
            f"{idea.confidence:.2f}",
            f"{idea.stake:,.0f}",
        )
    console.print(table)

    console.print("[bold]3/4 snapshot[/bold]")
    paths = snapshot_scan(result)
    console.print(f"  wrote {paths['latest']}")

    console.print("[bold]4/4 paper fills[/bold]")
    paper = apply_ideas(result["ideas"], top_n=paper_top, starting_cash=result["bankroll"])
    marked = mark_portfolio(
        {(i.exchange_id, i.side): i for i in result["ideas"]}
    )
    console.print(
        Panel.fit(
            f"paper cash: {paper['cash']:,.0f}\n"
            f"positions: {paper['position_count']}\n"
            f"MTM: {marked['mtm']:,.0f} | paper PnL: {marked['pnl']:,.0f}\n"
            f"fills applied: {len(paper['applied'])}",
            title="Paper portfolio",
        )
    )
    persist = edge_persistence(min_snapshots=1)
    if not persist.empty:
        console.print("\n[bold]Persistent edges (keep showing up)[/bold]")
        for _, row in persist.head(8).iterrows():
            console.print(
                f"• {row['last_title'][:50]} | "
                f"seen {int(row['appearances'])}x | "
                f"avg net {row['avg_net_edge']*100:.1f}¢"
            )


@app.command("research")
def research_cmd() -> None:
    """Snapshot current quotes/ideas and show persistent edges."""
    paths = snapshot_scan()
    console.print(f"[green]Saved[/green] {paths['latest']}")
    persist = edge_persistence(min_snapshots=1)
    if persist.empty:
        console.print("Not enough history yet — run `pmcup daily` again later.")
        return
    table = Table(title="Persistent edges")
    table.add_column("Race")
    table.add_column("Seen", justify="right")
    table.add_column("Avg net", justify="right")
    for _, row in persist.head(15).iterrows():
        table.add_row(
            str(row["last_title"])[:48],
            str(int(row["appearances"])),
            f"{row['avg_net_edge']*100:.1f}¢",
        )
    console.print(table)


@app.command("paper")
def paper_cmd(
    top: int = typer.Option(5, help="Apply top N current ideas as paper fills"),
    reset: bool = typer.Option(False, help="Wipe paper portfolio first"),
) -> None:
    """Paper-trade without placing real cup orders."""
    if reset:
        path = Path("data/paper/portfolio.json")
        if path.exists():
            path.unlink()
            console.print("[yellow]Paper portfolio reset[/yellow]")
    result = scan()
    out = apply_ideas(result["ideas"], top_n=top, starting_cash=result["bankroll"])
    marked = mark_portfolio({(i.exchange_id, i.side): i for i in result["ideas"]})
    console.print(
        Panel.fit(
            f"applied {len(out['applied'])} fills\n"
            f"cash {out['cash']:,.0f} | positions {out['position_count']}\n"
            f"MTM {marked['mtm']:,.0f} | PnL {marked['pnl']:,.0f}",
            title="Paper",
        )
    )


@app.command("leaderboard")
def leaderboard_cmd(limit: int = 20) -> None:
    """Show current cup standings."""
    with SuperMarketClient() as client:
        slug = settings.tournament_slug
        if not slug:
            ts = client.tournaments()
            slug = ts[0]["slug"]
        board = client.leaderboard(slug, period="all", limit=limit)
    table = Table(title=f"Leaderboard — {slug}")
    table.add_column("Rank")
    table.add_column("User")
    table.add_column("Value / PnL")
    for row in board.get("leaderboard") or []:
        table.add_row(
            str(row.get("rank") or row.get("position") or ""),
            str(row.get("username") or row.get("displayName") or row.get("profileId") or ""),
            str(row.get("portfolioValue") or row.get("pnl") or row.get("balance") or ""),
        )
    console.print(table)
    if board.get("myRank") is not None:
        console.print(f"Your rank: [bold]{board['myRank']}[/bold]")


@app.command("positions")
def positions_cmd() -> None:
    """Show open positions."""
    with SuperMarketClient() as client:
        slug = settings.tournament_slug or None
        if not slug:
            ts = client.tournaments()
            slug = ts[0]["slug"] if ts else None
        data = client.positions(slug)
    table = Table(title="Positions")
    table.add_column("Market")
    table.add_column("Qty")
    table.add_column("Avg")
    table.add_column("Mark")
    table.add_column("uPnL")
    for p in data.get("positions") or []:
        table.add_row(
            str(p.get("marketTitle"))[:48],
            str(p.get("quantity")),
            f"{p.get('avgCost')}",
            f"{p.get('currentPrice')}",
            f"{p.get('unrealizedPnl')}",
        )
    console.print(table)
    summary = data.get("summary") or {}
    console.print(summary)


@app.callback()
def main(
    ctx: typer.Context,
    env_file: Optional[Path] = typer.Option(None, help="Optional path to .env"),
) -> None:
    if env_file:
        # Lightweight reload support
        from dotenv import load_dotenv

        load_dotenv(env_file, override=True)


if __name__ == "__main__":
    app()
