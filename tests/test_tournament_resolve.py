from __future__ import annotations

from pathlib import Path

import pytest

from pmcup.config import Settings
from pmcup import scanner


class _BoomClient:
    def __init__(self, *, tournament=None, tournaments=None):
        self._tournament = tournament
        self._tournaments = tournaments

    def tournament(self, slug: str, *, max_retries: int | None = None):
        if self._tournament is None:
            raise RuntimeError("503")
        return self._tournament

    def tournaments(self, status: str = "any"):
        if self._tournaments is None:
            raise RuntimeError("503")
        return self._tournaments


def test_resolve_prefers_slug_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scanner, "_TOURNAMENT_CACHE", tmp_path / "cache.json")
    cfg = Settings(supermarket_api_key="t", tournament_slug="midterm-elections")
    client = _BoomClient(
        tournament={
            "id": "tid-1",
            "slug": "midterm-elections",
            "myBalance": 12.5,
            "initialBalance": 100_000,
        },
        tournaments=None,
    )
    slug, tid, bal = scanner._resolve_tournament(client, cfg)
    assert slug == "midterm-elections"
    assert tid == "tid-1"
    assert bal == 12.5
    assert (tmp_path / "cache.json").exists()


def test_resolve_falls_back_to_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / "cache.json"
    cache.write_text('{"slug":"midterm-elections","id":"cached-id","bankroll":100000}')
    monkeypatch.setattr(scanner, "_TOURNAMENT_CACHE", cache)
    cfg = Settings(supermarket_api_key="t", tournament_slug="midterm-elections")
    client = _BoomClient(tournament=None, tournaments=None)
    slug, tid, bal = scanner._resolve_tournament(client, cfg)
    assert slug == "midterm-elections"
    assert tid == "cached-id"
    assert bal == 100_000.0
