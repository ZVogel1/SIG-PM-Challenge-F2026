from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .client import SuperMarketClient
from .config import Settings, settings
from .constraints import ideas_from_constraints
from .edge import dedupe_complement_markets, edge_from_fair, edge_from_flb
from .fair_probs import load_fair_probs
from .models import Quote, TradeIdea
from .portfolio import cash_and_equity

log = logging.getLogger("pmcup.scanner")

_TOURNAMENT_CACHE = Path("data/bots/tournament_cache.json")


def _tournament_cache_path() -> Path:
    return _TOURNAMENT_CACHE


def _load_tournament_cache(slug: str | None) -> tuple[str, str, float] | None:
    path = _tournament_cache_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cached_slug = str(raw.get("slug") or "")
    cached_id = raw.get("id")
    if not cached_slug or cached_id is None:
        return None
    if slug and cached_slug != slug:
        return None
    try:
        bankroll = float(raw.get("bankroll") or raw.get("myBalance") or 100_000)
    except (TypeError, ValueError):
        bankroll = 100_000.0
    return cached_slug, str(cached_id), bankroll


def _save_tournament_cache(slug: str, tournament_id: str, bankroll: float) -> None:
    path = _tournament_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"slug": slug, "id": tournament_id, "bankroll": bankroll},
            indent=1,
        )
    )


def _match_bankroll(match: dict[str, Any]) -> float:
    return float(match.get("myBalance") or match.get("initialBalance") or 100_000)


def _resolve_tournament(client: SuperMarketClient, cfg: Settings) -> tuple[str, str, float]:
    """Resolve tournament id without depending on the flaky /tournaments list."""
    slug = cfg.tournament_slug
    match: dict[str, Any] | None = None

    # Prefer direct slug lookup — the list endpoint 503s more often.
    # When a cache exists, keep retries short so a 503 doesn't eat the cycle.
    if slug:
        cached_first = _load_tournament_cache(slug)
        try:
            raw = client.tournament(slug, max_retries=2 if cached_first else None)
            match = raw.get("data") if isinstance(raw, dict) and "data" in raw and "id" not in raw else raw
            if not isinstance(match, dict) or match.get("id") is None:
                match = None
        except Exception as exc:  # noqa: BLE001
            log.warning("tournament(%s) failed: %s", slug, exc)
            if cached_first:
                log.warning("using cached tournament %s id=%s", cached_first[0], cached_first[1])
                return cached_first

    if match is None:
        try:
            tournaments = client.tournaments(status="any")
        except Exception as exc:  # noqa: BLE001
            log.warning("tournaments list failed: %s", exc)
            tournaments = []
        if tournaments:
            if slug:
                match = next((t for t in tournaments if t.get("slug") == slug), None)
                if match is None:
                    names = ", ".join(f"{t.get('slug')} ({t.get('name')})" for t in tournaments)
                    raise SystemExit(f"Tournament slug '{slug}' not found. Available: {names}")
            else:
                match = next(
                    (
                        t
                        for t in tournaments
                        if any(
                            k in (t.get("name") or "").lower() + (t.get("slug") or "").lower()
                            for k in ("midterm", "prediction", "cup", "election")
                        )
                    ),
                    tournaments[0],
                )
                slug = match["slug"]

    if match is None:
        cached = _load_tournament_cache(slug or None)
        if cached:
            log.warning("using cached tournament %s id=%s", cached[0], cached[1])
            return cached
        raise SystemExit(
            "No tournaments visible on this API key (and no cache). "
            "Register/activate your account first."
        )

    slug = str(match.get("slug") or slug)
    tournament_id = str(match["id"])
    bankroll = _match_bankroll(match)
    _save_tournament_cache(slug, tournament_id, bankroll)
    return slug, tournament_id, bankroll


def build_quotes(
    markets: list[dict[str, Any]],
    prices: list[dict[str, Any]],
) -> list[Quote]:
    price_by_ex = {str(p["exchangeId"]): p for p in prices}
    quotes: list[Quote] = []
    for m in markets:
        title = m.get("title") or ""
        mid = str(m.get("id"))
        for ex in m.get("exchanges") or []:
            eid = str(ex.get("id"))
            p = price_by_ex.get(eid, {})
            option = ex.get("option")
            if option and str(option).upper() not in {"YES", "Y", "TRUE", "1"}:
                if len(m.get("exchanges") or []) > 1:
                    continue
            quotes.append(
                Quote(
                    market_id=mid,
                    market_title=title,
                    exchange_id=eid,
                    option=option,
                    latest_price=_num(p.get("latestPrice", ex.get("latestPrice"))),
                    best_bid=_num(p.get("bestBid")),
                    best_ask=_num(p.get("bestAsk")),
                    spread=_num(p.get("spread")),
                )
            )
    return quotes


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def scan(cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or settings
    with SuperMarketClient(cfg) as client:
        slug, tournament_id, listed_balance = _resolve_tournament(client, cfg)
        account: dict[str, Any] = {}
        try:
            account = client.account()
        except Exception as exc:  # noqa: BLE001
            # Account endpoint flakes independently; size off tournament cash.
            log.warning("account() failed, using tournament balance: %s", exc)
            account = {"balance": listed_balance}
        positions: dict[str, Any] = {"positions": []}
        try:
            positions = client.positions(slug)
        except Exception:  # noqa: BLE001
            positions = {"positions": []}

        # Size/caps off equity (cash + marks), not cash alone — otherwise the
        # bot locks itself out after deploying into positions.
        cash, equity = cash_and_equity(account, positions, fallback=listed_balance)
        bankroll = max(equity, listed_balance, cash)

        markets = client.list_tournament_markets(slug, status="open")
        if not markets:
            markets = client.list_markets(tournament_id=tournament_id, status="open")

        exchange_ids = [
            str(ex["id"])
            for m in markets
            for ex in (m.get("exchanges") or [])
            if ex.get("id") is not None
        ]
        prices = client.bulk_prices(exchange_ids, tournament_id=tournament_id) if exchange_ids else []
        quotes = build_quotes(markets, prices)
        fair = load_fair_probs()

        ideas: list[TradeIdea] = []
        missing_forecasts: list[dict[str, str]] = []
        for q in quotes:
            if q.exchange_id in fair:
                fp = fair[q.exchange_id]
                idea = edge_from_fair(
                    q,
                    fp.fair_yes,
                    bankroll=bankroll,
                    kelly_mult=cfg.tournament_kelly_mult,
                    max_frac=cfg.max_position_frac,
                    min_edge=cfg.min_edge,
                    confidence=fp.confidence,
                    min_net_edge=cfg.min_edge,
                    market_blend_strength=(
                        cfg.market_blend_strength if cfg.blend_toward_market else 0.0
                    ),
                )
            else:
                missing_forecasts.append(
                    {
                        "exchange_id": q.exchange_id,
                        "title": q.market_title[:80],
                    }
                )
                idea = edge_from_flb(
                    q,
                    bankroll=bankroll,
                    kelly_mult=cfg.tournament_kelly_mult,
                    max_frac=cfg.max_position_frac,
                )
            if idea:
                ideas.append(idea)

        try:
            constraints = client.constraints(
                violations_only=True,
                min_violation=0.01,
                tournament_id=tournament_id,
            )
            c_ideas = ideas_from_constraints(
                constraints,
                bankroll=bankroll,
                max_frac=cfg.max_position_frac,
            )
            for idea in c_ideas:
                idea.score = max(idea.score, abs(idea.edge) * idea.stake * 1.1)
                idea.net_edge = max(idea.net_edge, idea.edge)
            ideas.extend(c_ideas)
        except Exception as exc:  # noqa: BLE001
            constraints = {"error": str(exc), "data": [], "violationsCount": 0}

        ranked = dedupe_complement_markets(ideas)

        try:
            board = client.leaderboard(slug, period="all", limit=10)
        except Exception:  # noqa: BLE001
            board = {}

        return {
            "slug": slug,
            "tournament_id": tournament_id,
            "bankroll": bankroll,
            "cash": cash,
            "equity": equity,
            "positions": positions,
            "market_count": len(markets),
            "quote_count": len(quotes),
            "fair_prob_count": len(fair),
            "missing_forecasts": missing_forecasts[:50],
            "missing_forecast_count": len(missing_forecasts),
            "ideas": ranked,
            "quotes": quotes,
            "constraint_violations": constraints.get("violationsCount", 0),
            "account": account,
            "leaderboard": board,
            "dry_run": cfg.dry_run,
        }
