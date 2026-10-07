"""Settings shared by the data scripts."""

import pandas as pd

# Archive timestamp convention per provider (the provider column of data/stations.csv), each from a
# scripts/check_timestamps.py run. True = a reading is stamped at the end of the period it covers.
# The pull skips stations whose provider is missing here: check one of its stations first, never default.
STAMPED_AT_END = {
    "CPCB": True,  # station 235, 2025-11-12, 15-min: 84/84 rows
    "caaqm": True,  # station 7044, 2022-10-12, hourly: 16/16 rows
    "AirNow": False,  # station 8118 (US Embassy), 2023-11-14, hourly: 23/23 rows
}

STILL_REPORTING = pd.Timestamp("2026-09-01", tz="UTC")


def model_stations() -> list[int]:
    """Still reporting, with at least 60% coverage in 2025-26: the set for the backtest, final model and Lambdas."""
    s = pd.read_csv("data/stations.csv")
    cov = pd.read_csv("outputs/coverage.csv")
    good = cov.loc[(cov["winter"] == "2025-26") & (cov["coverage"] >= 0.6), "location_id"]
    keep = (pd.to_datetime(s["last_utc"], utc=True) >= STILL_REPORTING) & s["location_id"].isin(good)
    return sorted(int(i) for i in s.loc[keep, "location_id"])
