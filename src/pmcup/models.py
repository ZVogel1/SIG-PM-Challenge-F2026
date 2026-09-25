from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Quote:
    market_id: str
    market_title: str
    exchange_id: str
    option: str | None
    latest_price: float | None
    best_bid: float | None
    best_ask: float | None
    spread: float | None

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        if self.latest_price is not None:
            return self.latest_price
        if self.best_ask is not None:
            return self.best_ask
        if self.best_bid is not None:
            return self.best_bid
        return None

    def buy_yes_price(self) -> float | None:
        """Price to lift the offer for YES."""
        if self.best_ask is not None:
            return self.best_ask
        return self.mid

    def buy_no_price(self) -> float | None:
        """
        Price to buy NO.
        If YES bid is B, NO ask ≈ 1 - B (selling YES at bid = buying NO).
        """
        if self.best_bid is not None:
            return 1.0 - self.best_bid
        mid = self.mid
        return None if mid is None else 1.0 - mid

    def implied_spread(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return max(0.0, self.best_ask - self.best_bid)
        if self.spread is not None:
            return max(0.0, self.spread)
        return 0.05  # conservative when book is one-sided


@dataclass
class TradeIdea:
    """A single recommended trade, explained in plain language."""

    market_id: str
    market_title: str
    exchange_id: str
    side: str  # yes | no
    action: str  # buy | sell
    market_price: float
    fair_prob: float
    edge: float
    size: int
    stake: float
    rationale: str
    source: str  # fair_prob | flb | constraint
    tags: list[str] = field(default_factory=list)
    net_edge: float = 0.0  # edge after half-spread / costs
    spread: float = 0.0
    race_key: str = ""
    confidence: float = 1.0  # 0-1, polls > fundamentals
    score: float = 0.0  # ranking score

    @property
    def plain_english(self) -> str:
        direction = f"{self.action.upper()} {self.side.upper()}"
        pct_edge = abs(self.edge) * 100
        pct_net = abs(self.net_edge) * 100
        return (
            f"{direction} {self.size} shares of «{self.market_title}» "
            f"@ ~{self.market_price:.0%} | fair≈{self.fair_prob:.0%} | "
            f"raw edge {pct_edge:.1f}¢ | net {pct_net:.1f}¢ | "
            f"stake {self.stake:,.0f} SUSQies — {self.rationale}"
        )
