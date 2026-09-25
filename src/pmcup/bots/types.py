from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OrderProposal:
    bot: str
    exchange_id: str
    market_id: str
    market_title: str
    side: str  # yes | no
    action: str  # buy | sell
    quantity: int
    price: float | None
    reason: str
    priority: float
    race_key: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.exchange_id}:{self.side}:{self.action}"
