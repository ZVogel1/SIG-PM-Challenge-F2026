from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..client import SuperMarketClient
from ..config import Settings
from ..notify import notify_order_failures
from ..paper_scoreboard import record_paper_fills
from ..portfolio import cap_buy_quantity, exposure_by_race
from ..sizing import round_tick
from ..trading_window import window_status
from .types import OrderProposal

log = logging.getLogger("pmcup.bots")


def bots_dir() -> Path:
    path = Path("data/bots")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stable_idempotency(p: OrderProposal, tournament_id: str) -> str:
    """Stable key within a short window so HTTP retries don't double-submit."""
    # Bucket to ~2 minutes so intentional re-quotes later still get a new key
    bucket = int(datetime.now(timezone.utc).timestamp() // 120)
    raw = (
        f"{tournament_id}|{p.exchange_id}|{p.side}|{p.action}|"
        f"{p.quantity}|{p.price}|{bucket}|{p.bot}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Executor:
    """Places orders for merged bot proposals with safety caps."""

    def __init__(
        self,
        client: SuperMarketClient,
        cfg: Settings,
        tournament_id: str,
        *,
        bankroll: float = 100_000.0,
        positions_payload: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.tournament_id = tournament_id
        self.bankroll = bankroll
        self.positions_payload = positions_payload or {"positions": []}
        self.decisions_path = bots_dir() / "decisions.jsonl"

    def execute(self, proposals: list[OrderProposal]) -> list[dict[str, Any]]:
        exposure = exposure_by_race(self.positions_payload)
        # Highest priority first, unique by exchange+side+action
        uniq: dict[str, OrderProposal] = {}
        for p in sorted(proposals, key=lambda x: x.priority, reverse=True):
            if p.quantity <= 0:
                continue
            if p.key not in uniq:
                uniq[p.key] = p

        adjusted: list[OrderProposal] = []
        for p in uniq.values():
            qty = p.quantity
            if p.action == "buy":
                qty = cap_buy_quantity(
                    quantity=qty,
                    price=p.price,
                    race_key=p.race_key or p.exchange_id,
                    bankroll=self.bankroll,
                    exposure=exposure,
                    max_race_frac=self.cfg.max_race_exposure_frac,
                    max_position_frac=self.cfg.max_position_frac,
                )
                if qty <= 0:
                    log.info(
                        "Skip buy (exposure cap): %s race=%s",
                        p.market_title[:50],
                        p.race_key,
                    )
                    continue
                # Reserve room for later proposals in this same cycle
                px = float(p.price) if p.price is not None and p.price > 0 else 0.5
                key = p.race_key or p.exchange_id
                exposure[key] = exposure.get(key, 0.0) + qty * px
            if qty != p.quantity:
                p = OrderProposal(**{**p.__dict__, "quantity": qty})
            adjusted.append(p)

        chosen = adjusted[: self.cfg.bot_max_orders_per_cycle]
        results: list[dict[str, Any]] = []
        live = self.cfg.can_trade_live
        if self.cfg.latches_allow_live and not live:
            log.warning(
                "Live latches on but outside trading window — paper only. %s",
                window_status(),
            )

        failures: list[str] = []
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
                    idempotency_key=_stable_idempotency(p, self.tournament_id),
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
                failures.append(f"{p.bot} {p.action} {p.side} {p.market_title[:50]}: {exc}")
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

        try:
            record_paper_fills(results)
        except Exception:  # noqa: BLE001
            log.exception("Paper ledger update failed")
        if failures:
            try:
                notify_order_failures(failures)
            except Exception:  # noqa: BLE001
                log.exception("Order-failure notify failed")
        return results
