"""T4: is an archive reading stamped at the start or the end of the period it covers?

CPCB repeats each hourly value across its four 15-minute slots, so a value alone rarely identifies one API
period. Instead each archive row is lined up with the API period that ends at its timestamp, and with the one
that starts there; the convention is the one under which every lined-up value is the same.
"""

import os
from pathlib import Path

import pandas as pd
from openaq import OpenAQ

from clearhour.archive import fetch, list_month_keys, read_pm25

DAY = pd.Timestamp("2025-11-12")
RAW = Path("data/raw")
STILL_REPORTING = pd.Timestamp("2026-09-01", tz="UTC")
MIN_MATCHES = 10


def pick_station() -> tuple[pd.Series, pd.DataFrame]:
    """Still-reporting station with the longest history that has an archive file for DAY."""
    s = pd.read_csv("data/stations.csv")
    s = s[pd.to_datetime(s["last_utc"], utc=True) >= STILL_REPORTING]
    s = s.assign(first=pd.to_datetime(s["first_utc"], utc=True)).sort_values("first")
    for _, st in s.iterrows():
        keys = list_month_keys(int(st["location_id"]), DAY.year, DAY.month)
        keys = [k for k in keys if f"{DAY:%Y%m%d}" in k]
        if keys:
            return st, pd.concat([read_pm25(fetch(k, RAW)) for k in keys], ignore_index=True)
    raise SystemExit(f"No still-reporting station has an archive file for {DAY:%Y-%m-%d}")


def api_periods(sensor_id: int, lo: pd.Timestamp, hi: pd.Timestamp) -> pd.DataFrame:
    with OpenAQ(api_key=os.environ["OPENAQ_API_KEY"]) as client:
        res = client.measurements.list(
            sensors_id=sensor_id,
            data="measurements",
            datetime_from=lo.isoformat(),
            datetime_to=hi.isoformat(),
            limit=1000,
        )
    start = pd.to_datetime([m.period.datetime_from.utc for m in res.results], utc=True)
    interval = pd.to_timedelta([m.period.interval for m in res.results])
    return pd.DataFrame({"start": start, "end": start + interval, "value": [m.value for m in res.results]})


def line_up(archive: pd.DataFrame, api: pd.DataFrame, edge: str) -> pd.DataFrame:
    """Archive rows joined to the API period whose `edge` ("start" or "end") equals the archive timestamp."""
    m = archive.merge(api, left_on="ts_utc", right_on=edge, suffixes=("", "_api"))
    return m.assign(same_value=(m["value"] - m["value_api"]).abs() <= 0.01)


def main() -> None:
    st, archive = pick_station()
    archive = archive.dropna(subset=["value"])
    lo, hi = archive["ts_utc"].min() - pd.Timedelta(hours=2), archive["ts_utc"].max() + pd.Timedelta(hours=2)
    api = api_periods(int(st["pm25_sensor_id"]), lo, hi)

    print(f"Station {st['location_id']} {st['name']} (sensor {st['pm25_sensor_id']}, since {st['first']:%Y-%m-%d})")
    intervals = sorted(set(api["end"] - api["start"]))
    print(f"{DAY:%Y-%m-%d}: {len(archive)} archive rows, {len(api)} API rows, intervals {intervals}")
    lined = {stamp: line_up(archive, api, stamp.lower()) for stamp in ("END", "START")}
    for stamp, m in lined.items():
        print(f"if the archive stamps the {stamp}: {len(m)} rows line up, {m['same_value'].sum()} with the same value")
    verdicts = [s for s, m in lined.items() if len(m) >= MIN_MATCHES and m["same_value"].all()]
    if len(verdicts) != 1:
        raise SystemExit(f"NO VERDICT: need exactly one convention with {MIN_MATCHES}+ rows that all agree")

    m = lined[verdicts[0]].sort_values("ts_utc")
    table = m[["ts_utc", "start", "end", "value"]]
    table.columns = ["archive datetime (UTC)", "API period start", "API period end", "value"]
    print(table.head(12).to_string(index=False))
    print(f"... {len(m)} matched rows in all")
    print(f"ARCHIVE STAMPS = {verdicts[0]}")


if __name__ == "__main__":
    main()
