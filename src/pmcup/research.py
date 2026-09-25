from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .models import TradeIdea
from .scanner import scan


def history_dir() -> Path:
    path = Path("data/history")
    path.mkdir(parents=True, exist_ok=True)
    return path


def reports_dir() -> Path:
    path = Path("data/reports")
    path.mkdir(parents=True, exist_ok=True)
    return path


def snapshot_scan(result: dict[str, Any] | None = None) -> dict[str, Path]:
    """Persist quotes + ranked ideas for pre-open research."""
    result = result or scan()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hdir = history_dir()

    quote_rows = []
    for q in result.get("quotes") or []:
        quote_rows.append(
            {
                "ts": ts,
                "market_id": q.market_id,
                "exchange_id": q.exchange_id,
                "title": q.market_title,
                "mid": q.mid,
                "best_bid": q.best_bid,
                "best_ask": q.best_ask,
                "spread": q.implied_spread(),
            }
        )
    quotes_path = hdir / f"quotes_{ts}.csv"
    pd.DataFrame(quote_rows).to_csv(quotes_path, index=False)

    # append to rolling log
    rolling = hdir / "quotes_rolling.csv"
    qdf = pd.DataFrame(quote_rows)
    if rolling.exists():
        qdf.to_csv(rolling, mode="a", header=False, index=False)
    else:
        qdf.to_csv(rolling, index=False)

    idea_rows = [_idea_row(ts, idea) for idea in result.get("ideas") or []]
    ideas_path = hdir / f"ideas_{ts}.csv"
    pd.DataFrame(idea_rows).to_csv(ideas_path, index=False)

    rolling_ideas = hdir / "ideas_rolling.csv"
    idf = pd.DataFrame(idea_rows)
    if rolling_ideas.exists():
        idf.to_csv(rolling_ideas, mode="a", header=False, index=False)
    else:
        idf.to_csv(rolling_ideas, index=False)

    summary = {
        "ts": ts,
        "slug": result.get("slug"),
        "bankroll": result.get("bankroll"),
        "market_count": result.get("market_count"),
        "idea_count": len(idea_rows),
        "top": idea_rows[:10],
        "constraint_violations": result.get("constraint_violations"),
    }
    summary_path = reports_dir() / f"scan_{ts}.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    latest = reports_dir() / "latest_scan.json"
    latest.write_text(json.dumps(summary, indent=2))
    return {
        "quotes": quotes_path,
        "ideas": ideas_path,
        "summary": summary_path,
        "latest": latest,
    }


def _idea_row(ts: str, idea: TradeIdea) -> dict[str, Any]:
    return {
        "ts": ts,
        "race_key": idea.race_key,
        "market_id": idea.market_id,
        "exchange_id": idea.exchange_id,
        "title": idea.market_title,
        "action": idea.action,
        "side": idea.side,
        "market_price": idea.market_price,
        "fair_prob": idea.fair_prob,
        "edge": idea.edge,
        "net_edge": idea.net_edge,
        "spread": idea.spread,
        "size": idea.size,
        "stake": idea.stake,
        "confidence": idea.confidence,
        "score": idea.score,
        "source": idea.source,
    }


def edge_persistence(min_snapshots: int = 2) -> pd.DataFrame:
    """Races that keep showing up as top edges across snapshots."""
    path = history_dir() / "ideas_rolling.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    g = (
        df.groupby("race_key")
        .agg(
            appearances=("ts", "nunique"),
            avg_net_edge=("net_edge", "mean"),
            avg_score=("score", "mean"),
            last_title=("title", "last"),
        )
        .reset_index()
    )
    return g[g["appearances"] >= min_snapshots].sort_values("avg_score", ascending=False)
