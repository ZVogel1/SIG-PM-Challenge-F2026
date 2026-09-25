from __future__ import annotations

from pmcup.sizing import binary_kelly, tournament_size


def test_binary_kelly_no_edge() -> None:
    assert binary_kelly(0.4, 0.5) == 0.0


def test_binary_kelly_positive() -> None:
    k = binary_kelly(0.6, 0.5)
    assert 0.19 < k < 0.21


def test_tournament_size_respects_min_edge() -> None:
    shares, stake, edge = tournament_size(
        fair_prob=0.52,
        price=0.50,
        bankroll=100_000,
        min_edge=0.04,
    )
    assert shares == 0
    assert stake == 0.0
    assert abs(edge - 0.02) < 1e-9


def test_tournament_size_buys_with_edge() -> None:
    shares, stake, edge = tournament_size(
        fair_prob=0.60,
        price=0.50,
        bankroll=100_000,
        kelly_mult=1.0,
        max_frac=0.25,
        min_edge=0.04,
    )
    assert shares > 0
    assert stake > 0
    assert edge > 0
