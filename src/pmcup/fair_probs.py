from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

HEADERS = [
    "market_id",
    "exchange_id",
    "title",
    "fair_yes",
    "notes",
    "source",
    "forecast_source",
    "n_polls",
    "confidence",
]


@dataclass
class FairProb:
    fair_yes: float
    confidence: float = 1.0
    forecast_source: str = ""
    n_polls: int = 0


def example_path() -> Path:
    return Path("data/fair_probs.example.csv")


def default_path() -> Path:
    return Path("data/fair_probs.csv")


def ensure_example() -> Path:
    path = example_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        pd.DataFrame(columns=HEADERS).to_csv(path, index=False)
    return path


def load_fair_probs(path: Path | None = None) -> dict[str, FairProb]:
    """Map exchange_id -> FairProb."""
    path = path or default_path()
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    if df.empty or "exchange_id" not in df.columns or "fair_yes" not in df.columns:
        return {}
    out: dict[str, FairProb] = {}
    for _, row in df.iterrows():
        if pd.isna(row.get("fair_yes")):
            continue
        ex = str(row["exchange_id"]).strip()
        try:
            p = float(row["fair_yes"])
        except (TypeError, ValueError):
            continue
        if p > 1.0:
            p = p / 100.0
        conf = 0.7
        if "confidence" in df.columns and not pd.isna(row.get("confidence")):
            try:
                conf = float(row["confidence"])
            except (TypeError, ValueError):
                conf = 0.7
        else:
            src = str(row.get("forecast_source") or row.get("source") or "").lower()
            if "poll" in src:
                conf = 0.9
            elif "fundamental" in src:
                conf = 0.55
            elif "market" in src:
                conf = 0.4
        n_polls = 0
        if "n_polls" in df.columns and not pd.isna(row.get("n_polls")):
            try:
                n_polls = int(row["n_polls"])
            except (TypeError, ValueError):
                n_polls = 0
        out[ex] = FairProb(
            fair_yes=max(0.001, min(0.999, p)),
            confidence=max(0.1, min(1.0, conf)),
            forecast_source=str(row.get("forecast_source") or ""),
            n_polls=n_polls,
        )
    return out


def _seed_price(exchange: dict, quote: dict | None = None) -> float | None:
    quote = quote or {}
    bid = quote.get("bestBid")
    ask = quote.get("bestAsk")
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2.0
    for key in ("latestPrice", "bestBid", "bestAsk"):
        val = quote.get(key)
        if val is not None:
            return float(val)
    for key in ("latestPrice", "initialPrice"):
        val = exchange.get(key)
        if val is not None:
            return float(val)
    return None


def bootstrap_template(
    markets: list[dict],
    path: Path | None = None,
    *,
    price_by_exchange: dict[str, dict] | None = None,
) -> Path:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    price_by_exchange = price_by_exchange or {}
    rows = []
    for m in markets:
        title = m.get("title") or ""
        for ex in m.get("exchanges") or []:
            option = (ex.get("option") or "").upper()
            if option and option not in {"YES", "Y"}:
                continue
            eid = str(ex.get("id"))
            price = _seed_price(ex, price_by_exchange.get(eid))
            rows.append(
                {
                    "market_id": m.get("id"),
                    "exchange_id": ex.get("id"),
                    "title": title,
                    "fair_yes": "" if price is None else round(float(price), 3),
                    "notes": "Seeded from market — run pmcup update-probs",
                    "source": "market_seed",
                    "forecast_source": "",
                    "n_polls": 0,
                    "confidence": 0.4,
                }
            )
    pd.DataFrame(rows, columns=HEADERS).to_csv(path, index=False)
    return path
