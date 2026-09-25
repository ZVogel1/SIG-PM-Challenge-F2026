from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from .config import settings
from .fair_probs import default_path

VOTEPREDICTOR_BASE = "https://votepredictor.com/api/v1"
MANUAL_SOURCES = {"manual", "user", "locked", "human"}


def _is_protected_row(row: dict | pd.Series, *, protect: bool) -> bool:
    if not protect:
        return False
    locked = str(row.get("locked") or "").strip().lower()
    if locked in {"1", "true", "yes", "y"}:
        return True
    src = str(row.get("source") or "").strip().lower()
    if src in MANUAL_SOURCES:
        return True
    notes = str(row.get("notes") or "").lower()
    if notes.startswith("manual") or "[manual]" in notes:
        return True
    return False

STATE_ABBR = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


def _clamp(p: float) -> float:
    return max(0.01, min(0.99, float(p)))


def fetch_chamber_seats(chamber: str) -> dict[str, dict[str, Any]]:
    """Map race_id -> seat forecast row for senate/house."""
    r = httpx.get(f"{VOTEPREDICTOR_BASE}/chamber/{chamber}", timeout=60.0)
    r.raise_for_status()
    out: dict[str, dict[str, Any]] = {}
    for seat in r.json().get("seats") or []:
        rid = seat.get("race_id")
        if not rid:
            continue
        if seat.get("forecast") is None and seat.get("p_dem_win") is None:
            # some rows use top-level p_dem_win; others nest / null
            if seat.get("no_forecast"):
                continue
        out[rid] = seat
    return out


def fetch_race(race_id: str) -> dict[str, Any] | None:
    r = httpx.get(f"{VOTEPREDICTOR_BASE}/race/{race_id}", timeout=30.0)
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("forecast") is None:
        return None
    return data


def p_dem_from_seat(seat: dict[str, Any]) -> float | None:
    if seat.get("p_dem_win") is not None:
        return _clamp(seat["p_dem_win"])
    forecast = seat.get("forecast") or {}
    if forecast.get("p_dem_win") is not None:
        return _clamp(forecast["p_dem_win"])
    return None


def parse_market_title(title: str) -> dict[str, str] | None:
    """
    Parse cup market titles into party + race key.
    Examples:
      Will the Democratic Party win the Maine Senate?
      Will the Republican Party win the TN-05 House race?
      Will the Democratic Party win the Vermont Governor?
    """
    t = title.strip()
    m = re.match(
        r"^Will the (Democratic|Republican) Party win the (.+?)(\?)?$",
        t,
        flags=re.I,
    )
    if not m:
        return None
    party = m.group(1).title()
    rest = m.group(2).strip()

    # House: WI-03 House race / CA-12 House race
    hm = re.match(r"^([A-Z]{2})-(\d+)\s+House race$", rest, flags=re.I)
    if hm:
        return {
            "party": party,
            "office": "HOUSE",
            "location": f"{hm.group(1).upper()}-{int(hm.group(2))}",
            "race_id": f"2026_HOUSE_{hm.group(1).upper()}-{int(hm.group(2))}",
        }

    # Senate: Maine Senate / New Hampshire Senate
    sm = re.match(r"^(.+)\s+Senate$", rest, flags=re.I)
    if sm:
        state = sm.group(1).strip().lower()
        abbr = STATE_ABBR.get(state)
        if not abbr:
            return None
        return {
            "party": party,
            "office": "SEN",
            "location": abbr,
            "race_id": f"2026_SEN_{abbr}",
        }

    # Governor
    gm = re.match(r"^(.+)\s+Governor$", rest, flags=re.I)
    if gm:
        state = gm.group(1).strip().lower()
        abbr = STATE_ABBR.get(state)
        if not abbr:
            return None
        return {
            "party": party,
            "office": "GOV",
            "location": abbr,
            "race_id": f"2026_GOV_{abbr}",
        }

    return None


def seat_meta(seat: dict[str, Any]) -> dict[str, Any] | None:
    p = p_dem_from_seat(seat)
    if p is None:
        return None
    forecast = seat.get("forecast") or {}
    source = str(seat.get("source") or forecast.get("source") or "unknown")
    n_polls = int(seat.get("n_polls") or forecast.get("n_polls") or 0)
    if "poll" in source.lower():
        confidence = min(0.95, 0.75 + min(n_polls, 20) * 0.01)
    elif "fundamental" in source.lower():
        confidence = 0.55
    else:
        confidence = 0.65
    return {
        "p_dem_win": p,
        "forecast_source": source,
        "n_polls": n_polls,
        "confidence": confidence,
    }


def build_forecast_index() -> dict[str, dict[str, Any]]:
    """race_id -> {p_dem_win, forecast_source, n_polls, confidence}."""
    index: dict[str, dict[str, Any]] = {}
    for chamber in ("senate", "house"):
        seats = fetch_chamber_seats(chamber)
        for rid, seat in seats.items():
            meta = seat_meta(seat)
            if meta is not None:
                index[rid] = meta
    return index


def update_fair_probs_from_forecasts(
    path: Path | None = None,
    *,
    blend_with_market: float = 0.0,
    protect_manual: bool | None = None,
) -> dict[str, Any]:
    """
    Overwrite fair_yes using VotePredictor poll/fundamentals consensus.

    blend_with_market: 0 = pure forecast, 0.3 = 70% forecast + 30% current fair_yes.
    Rows with source manual/user/locked, locked=true, or notes starting with Manual
    are skipped when protect_manual is on (default from settings).
    """
    path = path or default_path()
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — run pmcup bootstrap-probs first")

    protect = (
        settings.protect_manual_fair_probs if protect_manual is None else protect_manual
    )

    df = pd.read_csv(path)
    # Ensure metadata columns exist with writable dtypes (pandas may infer float NaNs)
    if "forecast_source" not in df.columns:
        df["forecast_source"] = ""
    else:
        df["forecast_source"] = df["forecast_source"].astype("string").fillna("")
    if "n_polls" not in df.columns:
        df["n_polls"] = 0
    else:
        df["n_polls"] = pd.to_numeric(df["n_polls"], errors="coerce").fillna(0).astype(int)
    if "confidence" not in df.columns:
        df["confidence"] = 0.7
    else:
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce").fillna(0.7)
    if "locked" not in df.columns:
        df["locked"] = ""
    else:
        df["locked"] = df["locked"].astype("string").fillna("")

    # fair_yes must stay numeric-friendly for writes
    df["fair_yes"] = pd.to_numeric(df["fair_yes"], errors="coerce")
    df["notes"] = df.get("notes", "").astype("string").fillna("")
    df["source"] = df.get("source", "").astype("string").fillna("")

    forecast = build_forecast_index()
    matched = 0
    protected = 0
    missing_gov: set[str] = set()
    unmatched_titles: list[str] = []

    for i, row in df.iterrows():
        if _is_protected_row(row, protect=protect):
            protected += 1
            continue

        title = str(row.get("title") or "")
        parsed = parse_market_title(title)
        if not parsed:
            if title:
                unmatched_titles.append(title[:80])
            continue

        rid = parsed["race_id"]
        meta = forecast.get(rid)

        if meta is None and parsed["office"] == "GOV":
            race = fetch_race(rid)
            if race:
                meta = seat_meta(race)
                if meta is not None:
                    forecast[rid] = meta
            else:
                missing_gov.add(rid)

        if meta is None:
            continue

        p_dem = meta["p_dem_win"]
        fair = p_dem if parsed["party"] == "Democratic" else (1.0 - p_dem)
        fair = _clamp(fair)

        if blend_with_market > 0:
            try:
                market = float(row["fair_yes"])
                fair = (1 - blend_with_market) * fair + blend_with_market * market
                fair = _clamp(fair)
            except (TypeError, ValueError):
                pass

        df.at[i, "fair_yes"] = round(fair, 3)
        df.at[i, "source"] = "votepredictor"
        df.at[i, "forecast_source"] = str(meta["forecast_source"])
        df.at[i, "n_polls"] = int(meta["n_polls"])
        df.at[i, "confidence"] = float(round(meta["confidence"], 3))
        df.at[i, "notes"] = (
            f"Auto {rid} via VotePredictor ({meta['forecast_source']}, "
            f"n_polls={meta['n_polls']})."
        )
        matched += 1

    cols = [
        c
        for c in [
            "market_id",
            "exchange_id",
            "title",
            "fair_yes",
            "notes",
            "source",
            "forecast_source",
            "n_polls",
            "confidence",
            "locked",
        ]
        if c in df.columns
    ]
    extra = [c for c in df.columns if c not in cols]
    df[cols + extra].to_csv(path, index=False)
    # unique unmatched titles (cap for status/dashboard)
    seen: set[str] = set()
    missing_titles: list[str] = []
    for t in unmatched_titles:
        if t not in seen:
            seen.add(t)
            missing_titles.append(t)
        if len(missing_titles) >= 40:
            break
    return {
        "path": str(path),
        "matched": matched,
        "protected": protected,
        "forecast_cache_size": len(forecast),
        "missing_governor_ids": sorted(missing_gov),
        "unparsed_titles": missing_titles,
        "attribution": "Forecasts from votepredictor.com (free API, polls + fundamentals).",
    }
