from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..client import SuperMarketClient
from ..config import Settings
from ..edge import basket_from_title
from ..notify import notify_order_failures
from ..paper_scoreboard import record_paper_fills
from ..portfolio import (
    basket_cap_frac,
    cap_buy_quantity,
    cap_buy_to_cash,
    exposure_by_basket,
    exposure_by_race,
)
from ..sizing import round_tick
from ..trading_window import window_status
from .types import OrderProposal

log = logging.getLogger("pmcup.bots")


def bots_dir() -> Path:
    path = Path("data/bots")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stable_idempotency(p: OrderProposal, tournament_id: str, *, window_s: int = 120) -> str:
    """Stable key within a short window so HTTP retries don't double-submit."""
    bucket = int(datetime.now(timezone.utc).timestamp() // max(1, window_s))
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
        size_mult: float = 1.0,
        cash_available: float | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.tournament_id = tournament_id
        self.bankroll = bankroll
        self.positions_payload = positions_payload or {"positions": []}
        self.size_mult = max(0.0, min(1.0, float(size_mult)))
        self.cash_available = (
            float(cash_available) if cash_available is not None else float(bankroll)
        )
        self.decisions_path = bots_dir() / "decisions.jsonl"

    def _held_quantity(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for pos in self.positions_payload.get("positions") or []:
            if pos.get("settled"):
                continue
            out[str(pos.get("exchangeId"))] = abs(float(pos.get("quantity") or 0))
        return out

    def _cancel_stale_orders(self, orders: list[dict[str, Any]]) -> set[Any]:
        """Cancel resting limits older than order_stale_seconds."""
        stale_after = int(self.cfg.order_stale_seconds)
        if stale_after <= 0:
            return set()
        now = datetime.now(timezone.utc)
        canceled: set[Any] = set()
        for order in orders:
            if len(canceled) >= int(self.cfg.max_cancels_per_cycle):
                break
            created = order.get("createdAt")
            if not created:
                continue
            try:
                ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (now - ts).total_seconds() < stale_after:
                continue
            try:
                self.client.cancel_order(order.get("id"))
                canceled.add(order.get("id"))
            except Exception:  # noqa: BLE001
                log.warning("Could not cancel stale order %s", order.get("id"))
        if canceled:
            log.info("Canceled %s stale resting order(s)", len(canceled))
        return canceled

    def _marketable_price(self, exchange_id: str, side: str, action: str) -> float | None:
        """
        Price that should actually trade, taken from the live book.

        The book is quoted in YES terms, so NO orders need the complement.
        """
        try:
            book = self.client.orderbook(exchange_id, depth=1)
        except Exception:  # noqa: BLE001
            return None
        bid = book.get("bestBid")
        ask = book.get("bestAsk")
        try:
            bid = float(bid) if bid is not None else None
            ask = float(ask) if ask is not None else None
        except (TypeError, ValueError):
            return None
        if side == "yes":
            px = bid if action == "sell" else ask
        else:
            # NO sells hit the NO bid (1 - YES ask); NO buys lift the NO ask.
            px = (1.0 - ask) if (action == "sell" and ask is not None) else (
                (1.0 - bid) if bid is not None else None
            )
        if px is None or px <= 0:
            return None
        return round_tick(px)

    def execute(self, proposals: list[OrderProposal]) -> list[dict[str, Any]]:
        exposure = exposure_by_race(self.positions_payload)
        basket_exp = exposure_by_basket(self.positions_payload)
        size_mult = self.size_mult
        if size_mult < 1.0:
            log.warning("Circuit breaker active — buy size mult=%.2f", size_mult)

        # Resting orders: cancel stale ones, then never stack a duplicate on top
        resting: dict[tuple[str, str, str], int] = {}
        if self.cfg.can_trade_live:
            try:
                open_orders = self.client.open_orders(tournament_id=self.tournament_id)
            except Exception:  # noqa: BLE001
                log.exception("Could not load open orders")
                open_orders = []
            canceled = self._cancel_stale_orders(open_orders)
            for order in open_orders:
                if order.get("id") in canceled:
                    continue
                key = (
                    str(order.get("exchangeId")),
                    str(order.get("side")),
                    str(order.get("action")),
                )
                resting[key] = resting.get(key, 0) + int(order.get("quantity") or 0)
            if resting:
                log.info("Resting orders after cleanup: %s", len(resting))
        held = self._held_quantity()
        # Highest priority first, unique by exchange+side+action
        uniq: dict[str, OrderProposal] = {}
        for p in sorted(proposals, key=lambda x: x.priority, reverse=True):
            if p.quantity <= 0:
                continue
            if p.key not in uniq:
                uniq[p.key] = p

        ordered = sorted(
            uniq.values(),
            key=lambda x: (0 if x.action == "sell" else 1, -x.priority),
        )
        # Use real cash only — limit sells often rest unfilled, so don't
        # pretendsell proceeds are spendable in the same cycle.
        working_cash = float(self.cash_available)
        adjusted: list[OrderProposal] = []
        for p in ordered:
            qty = p.quantity
            resting_key = (str(p.exchange_id), str(p.side), str(p.action))
            resting_qty = resting.get(resting_key, 0)
            if resting_qty > 0:
                log.info(
                    "Skip %s — %s already resting on %s",
                    p.action,
                    resting_qty,
                    p.market_title[:50],
                )
                continue
            if p.action == "sell":
                # Never offer more than we actually hold
                held_qty = int(held.get(str(p.exchange_id), 0))
                if held_qty <= 0:
                    log.info("Skip sell — no position: %s", p.market_title[:50])
                    continue
                qty = min(qty, held_qty)
                if qty <= 0:
                    continue
            elif p.action == "buy":
                if size_mult < 1.0:
                    qty = max(0, int(qty * size_mult))
                title = p.market_title or ""
                basket = basket_from_title(title)
                qty = cap_buy_quantity(
                    quantity=qty,
                    price=p.price,
                    race_key=p.race_key or p.exchange_id,
                    bankroll=self.bankroll,
                    exposure=exposure,
                    max_race_frac=self.cfg.max_race_exposure_frac * size_mult,
                    max_position_frac=self.cfg.max_position_frac * size_mult,
                    basket=basket,
                    basket_exposure=basket_exp,
                    max_basket_frac=basket_cap_frac(basket, self.cfg) * size_mult,
                )
                cash_qty = cap_buy_to_cash(qty, p.price, working_cash)
                if cash_qty < qty:
                    log.info(
                        "Shrink buy to cash: %s %s→%s (cash=%.0f)",
                        p.market_title[:50],
                        qty,
                        cash_qty,
                        working_cash,
                    )
                    qty = cash_qty
                if qty <= 0:
                    log.info(
                        "Skip buy (exposure/basket/circuit/cash cap): %s race=%s basket=%s",
                        p.market_title[:50],
                        p.race_key,
                        basket,
                    )
                    continue
                px = float(p.price) if p.price is not None and p.price > 0 else 0.5
                key = p.race_key or p.exchange_id
                notional = qty * px
                exposure[key] = exposure.get(key, 0.0) + notional
                basket_exp[basket] = basket_exp.get(basket, 0.0) + notional
                working_cash = max(0.0, working_cash - notional)
            if qty != p.quantity:
                p = OrderProposal(**{**p.__dict__, "quantity": qty})
            adjusted.append(p)
            resting[resting_key] = resting.get(resting_key, 0) + qty

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
            # Price off the live book so limits actually trade instead of resting
            price = self._marketable_price(p.exchange_id, p.side, p.action)
            if price is None:
                price = None if p.price is None else round_tick(float(p.price))
            if p.action == "buy":
                # Never trust optimistic sell proceeds — refresh live cash
                if live:
                    try:
                        bal = self.client.account().get("balance")
                        if bal is not None:
                            self.cash_available = float(bal)
                    except Exception:  # noqa: BLE001
                        log.exception("Could not refresh cash before buy")
                qty = cap_buy_to_cash(p.quantity, price, self.cash_available)
                if qty <= 0:
                    log.info(
                        "Skip buy after cash check: %s (cash=%.0f)",
                        p.market_title[:50],
                        self.cash_available,
                    )
                    continue
                if qty != p.quantity:
                    p = OrderProposal(**{**p.__dict__, "quantity": qty})
            try:
                resp = self.client.place_order(
                    exchange_id=p.exchange_id,
                    side=p.side,
                    action=p.action,
                    quantity=p.quantity,
                    price=price,
                    tournament_id=self.tournament_id,
                    idempotency_key=_stable_idempotency(
                        p,
                        self.tournament_id,
                        window_s=max(120, int(self.cfg.bot_interval_seconds) * 2),
                    ),
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
                # Do not credit sell notionals here — unfilled limits leave cash at 0
                if p.action == "buy":
                    px = float(price) if price is not None and price > 0 else 0.5
                    self.cash_available = max(
                        0.0, self.cash_available - float(p.quantity) * px
                    )
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
