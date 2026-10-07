"""The hook stat: how much dirtier the assembly hour is than the cleanest school hour."""

from __future__ import annotations

import pandas as pd

IST = "Asia/Kolkata"
SCHOOL_HOURS = list(range(8, 14))  # hour-beginning bins, 08:00 to 13:00 IST
ASSEMBLY_HOUR = 8  # the 08:00-09:00 IST hour


def weekday_profile(
    hourly: pd.DataFrame, start: str, end: str, *, min_coverage: float = 0.6
) -> tuple[pd.Series, int]:
    """Mean PM2.5 by IST hour of day over Mon-Fri in [start, end), each station weighted equally.

    Stations with less than min_coverage of the window's weekday hours are left out.
    Returns the 24-hour profile and the number of stations used.
    """
    lo, hi = pd.Timestamp(start, tz=IST), pd.Timestamp(end, tz=IST)
    h = hourly[(hourly["hour_ist"] >= lo) & (hourly["hour_ist"] < hi)]
    h = h[h["hour_ist"].dt.dayofweek < 5]
    window = pd.date_range(lo, hi, freq="h", inclusive="left")
    expected = int((window.dayofweek < 5).sum())
    coverage = h.groupby("location_id").size() / expected
    keep = coverage[coverage >= min_coverage].index
    h = h[h["location_id"].isin(keep)]
    by_station = h.groupby(["location_id", h["hour_ist"].dt.hour])["pm25"].mean()
    profile = by_station.groupby(level=1).mean()
    profile.index.name = "hour_ist"
    return profile, len(keep)


def hook_numbers(profile: pd.Series) -> dict:
    school = profile.loc[SCHOOL_HOURS]
    assembly = float(profile.loc[ASSEMBLY_HOUR])
    best_hour = int(school.idxmin())
    best = float(school.min())
    return {
        "assembly_hour_ist": f"{ASSEMBLY_HOUR:02d}:00-{ASSEMBLY_HOUR + 1:02d}:00",
        "assembly_pm25": round(assembly, 1),
        "cleanest_school_hour_ist": f"{best_hour:02d}:00-{best_hour + 1:02d}:00",
        "cleanest_pm25": round(best, 1),
        "cut_pct": round((assembly - best) / assembly * 100, 1),
        "assembly_vs_cleanest_ratio": round(assembly / best, 2),
    }
