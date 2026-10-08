"""Forecast Lambda (container image): every station's school hours for one day, written to S3.

Event: {"source": "live"} at 05:30 IST, or {"source": "archive", "as_of": "2025-11-13"} to replay a past morning.
"""

from __future__ import annotations

import io
import json
import os
from importlib import resources

import boto3
import lightgbm as lgb
import numpy as np
import pandas as pd

from clearhour import features, meteo, store
from clearhour.constants import IST

_s3 = boto3.client("s3")
_MODEL: tuple[lgb.Booster, dict] | None = None


def model() -> tuple[lgb.Booster, dict]:
    global _MODEL
    if _MODEL is None:
        bucket = os.environ["DATA_BUCKET"]
        text = _s3.get_object(Bucket=bucket, Key="models/clearhour-lgbm.txt")["Body"].read().decode()
        meta = json.loads(_s3.get_object(Bucket=bucket, Key="models/features.json")["Body"].read())
        _MODEL = (lgb.Booster(model_str=text), meta)
    return _MODEL


def stations() -> list[dict]:
    return json.loads(resources.files("clearhour").joinpath("stations.json").read_text())


def run_day(event: dict) -> pd.Timestamp:
    ts = pd.Timestamp(event["as_of"]) if event.get("as_of") else pd.Timestamp.now(tz="UTC")
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    return ts.tz_convert(IST).normalize()


def live_obs(day: pd.Timestamp) -> pd.DataFrame:
    start = (day - pd.Timedelta(days=2)).tz_convert("UTC").isoformat()
    end = (day + pd.Timedelta(hours=6)).tz_convert("UTC").isoformat()
    rows = [
        {"location_id": s["location_id"], "hour_ist": pd.Timestamp(o["hour_utc"]).tz_convert(IST), "pm25": o["pm25"]}
        for s in stations()
        for o in store.get_obs(s["location_id"], start, end)
    ]
    return pd.DataFrame(rows, columns=["location_id", "hour_ist", "pm25"])


def archive_obs(day: pd.Timestamp) -> pd.DataFrame:
    obj = _s3.get_object(Bucket=os.environ["DATA_BUCKET"], Key="archive/pm25_hourly.parquet")
    df = pd.read_parquet(io.BytesIO(obj["Body"].read()), columns=["location_id", "hour_ist", "pm25"])
    df["hour_ist"] = df["hour_ist"].dt.tz_convert(IST)
    keep = {s["location_id"] for s in stations()}
    window = (df["hour_ist"] >= day - pd.Timedelta(days=2)) & (df["hour_ist"] < day + pd.Timedelta(days=1))
    return df[window & df["location_id"].isin(keep)]


def handler(event, context):
    day = run_day(event)
    source = event.get("source", "live")
    booster, meta = model()
    if meta["features"] != features.FEATURES:
        raise RuntimeError("model was trained on a different feature list; retrain")
    hourly = live_obs(day) if source == "live" else archive_obs(day)
    met = meteo.fetch((day - pd.Timedelta(days=1)).date().isoformat(), day.date().isoformat(), live=source == "live")
    blackout = features.blackout_for(hourly, day)
    trained_up_to = max(meta.get("blackouts_h", [0]))
    if blackout > trained_up_to:
        raise RuntimeError(f"the newest reading is {blackout} h before 04:00 IST; the model covers {trained_up_to} h")
    rows = features.build_rows(hourly, met, [day], blackout_h=blackout)
    if rows.empty:
        raise RuntimeError(f"no station had a reading within {features.MAX_STALENESS_H} h of the newest one")
    rows["location_id"] = pd.Categorical(rows["location_id"], categories=meta["stations"])
    rows["pred"] = np.expm1(booster.predict(rows[features.FEATURES]))
    preds: dict[str, dict[str, float]] = {}
    latest: dict[str, float] = {}  # stations with a reading from the night; the assembly call uses only these
    for r in rows.itertuples():
        preds.setdefault(str(r.location_id), {})[str(r.target_hour)] = round(float(r.pred), 1)
        if r.target_hour == 8 and r.lead_h <= features.FRESH_LEAD_H:
            latest[str(r.location_id)] = round(float(r.v_last), 1)
    body = {
        "day": str(day.date()),
        "source": source,
        "generated_at": pd.Timestamp.now(tz=IST).isoformat(timespec="seconds"),
        "median_lead_h": float(rows["lead_h"].median()),
        "blackout_h": blackout,
        "obs_through": (day + pd.Timedelta(hours=features.LATEST_OBS_HOUR - blackout)).isoformat(),
        "stations": preds,
        "latest": latest,
    }
    key = f"runs/{day.date()}/{source}/forecast.json"
    _s3.put_object(
        Bucket=os.environ["DATA_BUCKET"], Key=key, Body=json.dumps(body).encode(), ContentType="application/json"
    )
    print(
        json.dumps(
            {
                "day": body["day"],
                "source": source,
                "stations": len(preds),
                "blackout_h": blackout,
                "median_lead_h": body["median_lead_h"],
            }
        )
    )
    return {
        "day": body["day"],
        "source": source,
        "forecast_key": key,
        "stations": len(preds),
        "resend": bool(event.get("resend")),
    }
