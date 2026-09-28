from __future__ import annotations

from pathlib import Path

from pmcup.circuit_breaker import state_path, update_circuit_breaker
from pmcup.config import Settings


def test_circuit_trips_at_20_pct(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "bots").mkdir(parents=True)
    cfg = Settings(
        circuit_breaker_enabled=True,
        circuit_breaker_drawdown=0.20,
        circuit_breaker_size_mult=0.5,
        notify_on_errors=False,
        notify_on_stop=False,
    )
    s1 = update_circuit_breaker(100_000, cfg)
    assert s1["tripped"] is False
    assert s1["size_mult"] == 1.0
    assert s1["peak_equity"] == 100_000

    s2 = update_circuit_breaker(90_000, cfg)  # -10% — not yet
    assert s2["tripped"] is False

    s3 = update_circuit_breaker(80_000, cfg)  # -20%
    assert s3["tripped"] is True
    assert s3["size_mult"] == 0.5
    assert state_path().exists()


def test_circuit_hysteresis_reset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "bots").mkdir(parents=True)
    cfg = Settings(
        circuit_breaker_enabled=True,
        circuit_breaker_drawdown=0.20,
        circuit_breaker_size_mult=0.5,
        notify_on_errors=False,
        notify_on_stop=False,
    )
    update_circuit_breaker(100_000, cfg)
    update_circuit_breaker(80_000, cfg)
    # Still below recover threshold (10%): stay tripped
    mid = update_circuit_breaker(85_000, cfg)
    assert mid["tripped"] is True
    # Back above -10% from peak → reset
    ok = update_circuit_breaker(92_000, cfg)
    assert ok["tripped"] is False
    assert ok["size_mult"] == 1.0


def test_new_peak_resets_drawdown_baseline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "bots").mkdir(parents=True)
    cfg = Settings(
        circuit_breaker_enabled=True,
        circuit_breaker_drawdown=0.20,
        notify_on_errors=False,
        notify_on_stop=False,
    )
    update_circuit_breaker(100_000, cfg)
    update_circuit_breaker(120_000, cfg)  # new peak
    s = update_circuit_breaker(110_000, cfg)  # only ~8% off new peak
    assert s["peak_equity"] == 120_000
    assert s["tripped"] is False
