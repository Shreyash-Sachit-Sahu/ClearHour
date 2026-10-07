"""T5: four winters of hourly PM2.5 from the OpenAQ archive.

Writes data/processed/pm25_hourly.parquet and outputs/coverage.csv. Downloads are cached in data/raw/.
With --cached it rebuilds from data/raw alone, without listing S3.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from config import STAMPED_AT_END

from clearhour.archive import IST, fetch, list_month_keys, month_prefix, read_pm25, to_hourly

RAW = Path("data/raw")
OUT = Path("data/processed/pm25_hourly.parquet")
COVERAGE = Path("outputs/coverage.csv")
WINTERS = [2022, 2023, 2024, 2025]  # October of this year to January of the next
MONTHS = [(y, m) for w in WINTERS for y, m in ((w, 10), (w, 11), (w, 12), (w + 1, 1))] + [(2026, 2), (2026, 10)]


def label(winter: int) -> str:
    return f"{winter}-{(winter + 1) % 100:02d}"


def window(winter: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    return pd.Timestamp(f"{winter}-10-01", tz=IST), pd.Timestamp(f"{winter + 1}-02-01", tz=IST)


def cached_keys(location_id: int, year: int, month: int) -> list[str]:
    return sorted(str(p.relative_to(RAW)) for p in (RAW / month_prefix(location_id, year, month)).glob("*.csv.gz"))


def pull(stations: pd.DataFrame, cached: bool) -> tuple[pd.DataFrame, int, int]:
    """Hourly frames per station. All of a station's months are binned together, so an hour whose readings
    sit in two month files becomes one mean; raw rows live only for one station at a time."""
    frames, files, empty = [], 0, 0
    list_keys = cached_keys if cached else list_month_keys
    with ThreadPoolExecutor(max_workers=16) as pool:
        for st in stations.itertuples():
            if st.provider not in STAMPED_AT_END:
                print(f"  WARNING: skipped {st.location_id} {st.name}: provider {st.provider!r} not in config.py")
                continue
            paths = []
            for year, month in MONTHS:
                keys = list_keys(st.location_id, year, month)
                empty += not keys
                paths += pool.map(lambda k: fetch(k, RAW), keys)
            files += len(paths)
            if paths:
                readings = pd.concat([read_pm25(p) for p in paths], ignore_index=True)
                frames.append(to_hourly(readings, stamped_at_end=STAMPED_AT_END[st.provider]))
            print(f"  {st.location_id:>8}  {st.name[:45]:<45} {len(paths):>5} files")
    return pd.concat(frames, ignore_index=True), files, empty


def coverage(hourly: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    """Share of each winter's Oct-Jan hours with data, per station (0 for stations with no data)."""
    rows = []
    for winter in WINTERS:
        lo, hi = window(winter)
        in_window = hourly[(hourly["hour_ist"] >= lo) & (hourly["hour_ist"] < hi)]
        hours = in_window.groupby("location_id").size().reindex(stations["location_id"], fill_value=0)
        total = (hi - lo) / pd.Timedelta(hours=1)
        rows.append(
            pd.DataFrame(
                {
                    "location_id": stations["location_id"].to_numpy(),
                    "name": stations["name"].to_numpy(),
                    "winter": label(winter),
                    "hours": hours.to_numpy(),
                    "coverage": (hours.to_numpy() / total).round(3),
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def main() -> None:
    stations = pd.read_csv("data/stations.csv")
    cached = "--cached" in sys.argv
    source = "data/raw only" if cached else "S3, cached in data/raw"
    print(f"Pulling {len(stations)} stations x {len(MONTHS)} months from {source}; stamped_at_end: {STAMPED_AT_END}")
    hourly, files, empty = pull(stations, cached)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    hourly.to_parquet(OUT, index=False)
    cov = coverage(hourly, stations)
    COVERAGE.parent.mkdir(parents=True, exist_ok=True)
    cov.to_csv(COVERAGE, index=False)

    first, last = hourly["hour_ist"].min(), hourly["hour_ist"].max()
    print(f"\n{len(hourly):,} hourly rows from {files:,} daily files")
    n_with_data = hourly["location_id"].nunique()
    print(f"{n_with_data} of {len(stations)} stations have data, {first:%Y-%m-%d} to {last:%Y-%m-%d}")
    print(f"gaps: {empty} station-months with no archive files; median readings per hour {hourly['n'].median():.0f}")
    good = cov[cov["coverage"] >= 0.6].groupby("winter").size().reindex([label(w) for w in WINTERS], fill_value=0)
    print("stations with >= 60% coverage: " + " · ".join(f"{w} {n}" for w, n in good.items()))
    overall = cov.groupby(["location_id", "name"])["coverage"].mean().nsmallest(5)
    print("worst coverage, mean of the four winters:")
    print(overall.round(3).to_string())
    print(f"wrote {OUT} and {COVERAGE}")


if __name__ == "__main__":
    main()
