from __future__ import annotations

from pathlib import Path

import pandas as pd

from pmcup.forecasts import _is_protected_row, update_fair_probs_from_forecasts


def test_is_protected_manual_source() -> None:
    assert _is_protected_row({"source": "manual", "notes": ""}, protect=True)
    assert _is_protected_row({"source": "votepredictor", "locked": "true"}, protect=True)
    assert _is_protected_row({"source": "votepredictor", "notes": "Manual override"}, protect=True)
    assert not _is_protected_row({"source": "votepredictor", "notes": "auto"}, protect=True)
    assert not _is_protected_row({"source": "manual"}, protect=False)


def test_update_skips_protected(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "fair_probs.csv"
    pd.DataFrame(
        [
            {
                "market_id": 1,
                "exchange_id": 11,
                "title": "Will the Democratic Party win the Maine Senate?",
                "fair_yes": 0.55,
                "notes": "Manual lock",
                "source": "manual",
                "forecast_source": "",
                "n_polls": 0,
                "confidence": 0.9,
                "locked": "",
            }
        ]
    ).to_csv(path, index=False)

    monkeypatch.setattr(
        "pmcup.forecasts.build_forecast_index",
        lambda: {"2026_SEN_ME": {"p_dem_win": 0.99, "forecast_source": "polls", "n_polls": 9, "confidence": 0.9}},
    )
    result = update_fair_probs_from_forecasts(path, protect_manual=True)
    assert result["protected"] == 1
    assert result["matched"] == 0
    df = pd.read_csv(path)
    assert float(df.iloc[0]["fair_yes"]) == 0.55
    assert df.iloc[0]["source"] == "manual"
