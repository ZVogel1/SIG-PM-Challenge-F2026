from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..client import SuperMarketClient
from ..config import Settings
from ..sizing import round_tick
from .types import OrderProposal

log = logging.getLogger("pmcup.bots")


def bots_dir() -> Path:
    path = Path("data/bots")
    path.mkdir(parents=True, exist_ok=True)
    return path


class Executor:
    """Places orders for merged bot proposals with safety caps."""

    def __init__(self, client: SuperMarketClient, cfg: Settings, tournament_id: str) -> None:
        self.client = client
        self.cfg = cfg
        self.tournament_id = tournament_id
        self.decisions_path = bots_dir() / "decisions.jsonl"

    def execute(self, proposals: list[OrderProposal]) -> list[dict[str, Any]]:
        # Highest priority first, unique by exchange+side+action
        uniq: dict[str, OrderProposal] = {}
        for p in sorted(proposals, key=lambda x: x.priority, reverse=True):
            if p.quantity <= 0:
                continue
            if p.key not in uniq:
                uniq[p.key] = p
        chosen = list(uniq.values())[: self.cfg.bot_max_orders_per_cycle]

        results: list[dict[str, Any]] = []
        live = self.cfg.can_trade_live
        for p in chosen:
            price = None if p.price is None else round_tick(p.price)
            try:
                resp = self.client.place_order(
                    exchange_id=p.exchange_id,
                    side=p.side,
                    action=p.action,
                    quantity=p.quantity,
                    price=price,
                    tournament_id=self.tournament_id,
                    dry_run=not live,
                )
                row = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "live": live,
                    "bot": p.bot,
                    "title": p.market_title,
                    "side": p.side,
                    "action": p.action,
                    "qty": p.quantity,
                    "price": price,
                    "reason": p.reason,
                    "priority": p.priority,
                    "response": resp,
                    "ok": True,
                }
            except Exception as exc:  # noqa: BLE001
                row = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "live": live,
                    "bot": p.bot,
                    "title": p.market_title,
                    "side": p.side,
                    "action": p.action,
                    "qty": p.quantity,
                    "price": price,
                    "reason": p.reason,
                    "ok": False,
                    "error": str(exc),
                }
                log.exception("Order failed: %s", p.market_title)
            results.append(row)
            with self.decisions_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            mode = "LIVE" if live else "PAPER/DRY"
            log.info(
                "[%s] %s %s %s x%s @ %s — %s",
                mode,
                p.bot,
                p.action.upper(),
                p.side.upper(),
                p.quantity,
                price,
                p.market_title[:60],
            )
        return results
