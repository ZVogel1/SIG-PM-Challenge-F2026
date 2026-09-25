from __future__ import annotations

import uuid
from typing import Any

import httpx

from .config import Settings, settings


class SuperMarketClient:
    """Thin wrapper around The Super Market REST API."""

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        self.api_key = self.cfg.require_api_key()
        self.base = self.cfg.supermarket_api_base.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.cfg.request_timeout_s,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SuperMarketClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        r = self._client.get(path, params=clean)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        r = self._client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    def account(self) -> dict[str, Any]:
        return self._get("/account")

    def tournaments(self, status: str = "any") -> list[dict[str, Any]]:
        data = self._get("/tournaments", status=status, limit=100)
        return data.get("data", [])

    def tournament(self, slug: str) -> dict[str, Any]:
        return self._get(f"/tournaments/{slug}")

    def list_tournament_markets(
        self,
        slug: str,
        *,
        status: str = "open",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self._get(
                f"/tournaments/{slug}/markets",
                status=status,
                limit=limit,
                cursor=cursor,
            )
            out.extend(page.get("data", []))
            pag = page.get("pagination") or {}
            if not pag.get("hasMore"):
                break
            cursor = pag.get("nextCursor")
            if not cursor:
                break
        return out

    def list_markets(
        self,
        *,
        tournament_id: str | None = None,
        status: str = "open",
        category: str | None = "election-outcome",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self._get(
                "/markets",
                tournamentId=tournament_id,
                status=status,
                category=category,
                limit=limit,
                cursor=cursor,
            )
            out.extend(page.get("data", []))
            pag = page.get("pagination") or {}
            if not pag.get("hasMore"):
                break
            cursor = pag.get("nextCursor")
            if not cursor:
                break
        return out

    def bulk_prices(
        self,
        exchange_ids: list[str],
        *,
        tournament_id: str | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for i in range(0, len(exchange_ids), 100):
            chunk = exchange_ids[i : i + 100]
            payload = self._get(
                "/exchanges/prices",
                ids=",".join(chunk),
                tournamentId=tournament_id,
            )
            results.extend(payload.get("data", []))
        return results

    def orderbook(
        self,
        exchange_id: str,
        *,
        depth: int = 10,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/exchanges/{exchange_id}/orderbook",
            depth=depth,
            tournamentId=tournament_id,
        )

    def constraints(
        self,
        *,
        violations_only: bool = True,
        min_violation: float | None = 0.01,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/relationships/constraints",
            violationsOnly="true" if violations_only else "false",
            minViolation=min_violation,
            tournamentId=tournament_id,
        )

    def positions(self, slug: str | None = None) -> dict[str, Any]:
        if slug:
            return self._get(f"/tournaments/{slug}/portfolio/positions")
        return self._get("/portfolio/positions")

    def leaderboard(self, slug: str, *, period: str = "all", limit: int = 25) -> dict[str, Any]:
        return self._get(
            f"/tournaments/{slug}/leaderboard",
            period=period,
            limit=limit,
        )

    def place_order(
        self,
        *,
        exchange_id: str,
        side: str,
        action: str,
        quantity: int,
        price: float | None = None,
        tournament_id: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if dry_run is None:
            dry_run = self.cfg.dry_run
        body: dict[str, Any] = {
            "exchangeId": str(exchange_id),
            "side": side.lower(),
            "action": action.lower(),
            "quantity": int(quantity),
            "idempotencyKey": idempotency_key or str(uuid.uuid4()),
        }
        if price is not None:
            body["price"] = float(price)
        if tournament_id:
            body["tournamentId"] = tournament_id
        if dry_run:
            return {"dry_run": True, "would_post": body}
        return self._post("/orders", body)
