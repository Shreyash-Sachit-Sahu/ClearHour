"""T6 (revised): the hook stat on 2025-26 data.

Writes outputs/hook_stat.json, outputs/hook_chart.png and outputs/hour_profile_2025.csv.
"""

import json
from pathlib import Path

import pandas as pd

from clearhour import hook_chart
from clearhour.hook import ASSEMBLY_HOUR, IST, SCHOOL_HOURS, hook_numbers, weekday_profile

HOURLY = Path("data/processed/pm25_hourly.parquet")
OUT = Path("outputs")
START, END = "2025-11-01", "2026-01-01"
US_EMBASSY = 8118
# 1-3 Nov 2023 carries two interleaved series at 8118 (docs/DECISIONS.md), so its 2023 window starts on 4 Nov.
EMBASSY_WINDOWS = {
    2022: ("2022-11-01", "2023-01-01"),
    2023: ("2023-11-04", "2024-01-01"),
    2024: ("2024-11-01", "2025-01-01"),
    2025: ("2025-11-01", "2026-01-01"),
}


def window(start: str, end: str) -> dict:
    return {"from": start, "to": str((pd.Timestamp(end) - pd.Timedelta(days=1)).date())}


def day_level(hourly: pd.DataFrame, start: str, end: str) -> dict:
    """Weekday station-days with all six school hours: how much cleaner the best school hour is than 08:00."""
    t = hourly["hour_ist"]
    h = hourly[(t >= pd.Timestamp(start, tz=IST)) & (t < pd.Timestamp(end, tz=IST)) & (t.dt.dayofweek < 5)]
    h = h[h["hour_ist"].dt.hour.isin(SCHOOL_HOURS)]
    wide = (
        h.assign(day=h["hour_ist"].dt.normalize(), hour=h["hour_ist"].dt.hour)
        .pivot_table(index=["location_id", "day"], columns="hour", values="pm25")
        .reindex(columns=SCHOOL_HOURS)
        .dropna()
    )
    wide = wide[wide[ASSEMBLY_HOUR] > 0]
    cut = (wide[ASSEMBLY_HOUR] - wide.min(axis=1)) / wide[ASSEMBLY_HOUR]
    return {
        **window(start, end),
        "weekdays_only": True,
        "stations": int(wide.index.get_level_values("location_id").nunique()),
        "station_days": len(wide),
        "median_cut_day_pct": round(float(cut.median()) * 100, 1),
        "share_cut_day_ge_20pct": round(float((cut >= 0.2).mean()), 3),
        "share_cleanest_hour_12_or_13": round(float(wide.idxmin(axis=1).isin([12, 13]).mean()), 3),
    }


def embassy_years(hourly: pd.DataFrame) -> dict:
    station = hourly[hourly["location_id"] == US_EMBASSY]
    years = {}
    for year, (start, end) in EMBASSY_WINDOWS.items():
        profile, n = weekday_profile(station, start, end)
        if not n:
            years[str(year)] = {**window(start, end), "skipped": "under 60% coverage"}
            continue
        profile = profile.reindex(range(24))  # an hour with no readings stays NaN and is left out of the minimum
        missing = [h for h in SCHOOL_HOURS if pd.isna(profile.loc[h])]
        years[str(year)] = {**window(start, end), **hook_numbers(profile), "missing_school_hours": missing}
    years["2023"]["note"] = (
        "starts 4 Nov: 1-3 Nov 2023 carries two interleaved series at this monitor. 11:00 IST has readings on only "
        "2 days, so the cleanest hour is taken over the other five school hours (the cut is a lower bound)."
    )
    return {"timestamp_convention": "START (AirNow; checked on 2023-11-14, 23/23 rows)", "years": years}


def main() -> None:
    hourly = pd.read_parquet(HOURLY)
    profile, n_stations = weekday_profile(hourly, START, END)
    nums = hook_numbers(profile)
    hook_chart.draw(profile, nums, n_stations, "Nov–Dec 2025", OUT / "hook_chart.png")
    profile.round(1).rename("pm25").reset_index().to_csv(OUT / "hour_profile_2025.csv", index=False)

    result = {
        "nov_dec_2025": {**window(START, END), "weekdays_only": True, "n_stations": n_stations, **nums},
        "day_level_nov_dec_2025": day_level(hourly, START, END),
        "us_embassy_8118": embassy_years(hourly),
    }
    (OUT / "hook_stat.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("wrote outputs/hook_stat.json, outputs/hook_chart.png, outputs/hour_profile_2025.csv")
    if result["day_level_nov_dec_2025"]["median_cut_day_pct"] <= 5:
        raise SystemExit("STOP: the median cut_day is 5% or less")


if __name__ == "__main__":
    main()
