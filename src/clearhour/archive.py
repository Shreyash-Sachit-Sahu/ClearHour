"""OpenAQ archive on AWS: public bucket, read anonymously."""

from __future__ import annotations

from pathlib import Path

import boto3
import pandas as pd
from botocore import UNSIGNED
from botocore.config import Config

BUCKET = "openaq-data-archive"
IST = "Asia/Kolkata"

_s3 = boto3.client(
    "s3",
    region_name="us-east-1",
    config=Config(signature_version=UNSIGNED, retries={"max_attempts": 10, "mode": "adaptive"}),
)


def month_prefix(location_id: int, year: int, month: int) -> str:
    return f"records/csv.gz/locationid={location_id}/year={year}/month={month:02d}/"


def list_month_keys(location_id: int, year: int, month: int) -> list[str]:
    """Every daily file for one location-month. Lists the prefix rather than guessing file names."""
    keys: list[str] = []
    kwargs = {"Bucket": BUCKET, "Prefix": month_prefix(location_id, year, month)}
    while True:
        resp = _s3.list_objects_v2(**kwargs)
        keys += [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".csv.gz")]
        if not resp.get("IsTruncated"):
            return keys
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]


def fetch(key: str, raw_dir: Path) -> Path:
    """Download once; later runs read the cached copy."""
    dest = raw_dir / key
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        _s3.download_file(BUCKET, key, str(part))
        part.rename(dest)
    return dest


def read_pm25(path: Path) -> pd.DataFrame:
    """One daily file -> PM2.5 rows with a UTC timestamp.

    The docs' column table says sensor_id/unit while sample files say sensors_id/units,
    so names are normalised before use.
    """
    df = pd.read_csv(path, compression="gzip")
    df = df.rename(columns={"sensors_id": "sensor_id", "units": "unit"})
    df = df[df["parameter"].astype(str).str.lower().isin(["pm25", "pm2.5"])]
    return pd.DataFrame(
        {
            "location_id": df["location_id"].astype("int64"),
            "ts_utc": pd.to_datetime(df["datetime"], utc=True, format="ISO8601"),
            "value": pd.to_numeric(df["value"], errors="coerce"),
        }
    )


def to_hourly(readings: pd.DataFrame, *, stamped_at_end: bool = True, valid_max: float = 1000.0) -> pd.DataFrame:
    """Hourly mean PM2.5 per location, keyed by the hour the reading covers (hour-beginning).

    stamped_at_end=True means a value stamped 09:00 covers 08:00-09:00 and lands in the 08:00 hour.
    Confirm the convention with scripts/check_timestamps.py and record it in docs/DECISIONS.md.
    """
    r = readings.dropna(subset=["value"])
    r = r[(r["value"] >= 0) & (r["value"] <= valid_max)]
    shift = pd.Timedelta(minutes=1) if stamped_at_end else pd.Timedelta(0)
    # Floor on the IST clock: IST is UTC+5:30, so UTC hours would cut every IST hour in half.
    ist_hour = (r["ts_utc"] - shift).dt.tz_convert(IST).dt.floor("h")
    hourly = (
        r.assign(hour_utc=ist_hour.dt.tz_convert("UTC"))
        .groupby(["location_id", "hour_utc"], as_index=False)
        .agg(pm25=("value", "mean"), n=("value", "size"))
    )
    hourly["hour_ist"] = hourly["hour_utc"].dt.tz_convert(IST)
    return hourly
