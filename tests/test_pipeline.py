"""End to end on mocked AWS (moto): forecast -> decide -> send -> reply, with WhatsApp in dry-run mode."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import pandas as pd
import pytest
from moto import mock_aws
from synthetic import make_synthetic

from clearhour import features, site, store
from clearhour.model import fit, with_station_category

IST = ZoneInfo("Asia/Kolkata")
TABLE, BUCKET = "clearhour-test", "clearhour-test-data"
STATIONS = [
    {"location_id": 100 + i, "pm25_sensor_id": 1000 + i, "name": f"S{i}", "lat": 28.6, "lon": 77.2} for i in range(3)
]
SCHOOLS = [
    {"id": "node/1", "name": "Sarvodaya Vidyalaya", "lat": 28.6, "lon": 77.2, "near": [[100, 0.7], [101, 0.3]]},
    {"id": "node/2", "name": "Far School", "lat": 28.8, "lon": 77.0, "near": [[999, 1.0]]},  # no forecast
]


@pytest.fixture
def aws(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": "ap-south-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "TABLE_NAME": TABLE,
        "DATA_BUCKET": BUCKET,
        "WA_MODE": "dry_run",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3").create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        monkeypatch.setattr(store, "_TABLE", None)
        monkeypatch.setattr(site, "_S3", None)
        monkeypatch.setattr(site, "stations", lambda: STATIONS)
        yield


def _seed_model_and_obs(monkeypatch, day: pd.Timestamp, obs_until: pd.Timestamp | None = None):
    from clearhour.handlers import forecast

    hourly, met = make_synthetic(n_stations=3, start="2025-10-01", end="2025-11-20")
    stations = [int(s) for s in sorted(hourly["location_id"].unique())]  # plain ints: they go into JSON
    train_days = pd.date_range("2025-10-03", "2025-11-10", tz="Asia/Kolkata")
    s3 = boto3.client("s3")
    for suffix, blackouts in (("", [0]), ("-stale", [12, 24])):  # the fresh and the stale model
        rows = pd.concat(
            [
                with_station_category(features.build_rows(hourly, met, train_days, blackout_h=b), stations)
                for b in blackouts
            ],
            ignore_index=True,
        )
        assert not rows.empty
        booster = fit(rows, num_rounds=30)
        meta = {"features": features.FEATURES, "stations": stations, "blackouts_h": blackouts}
        s3.put_object(Bucket=BUCKET, Key=f"models/clearhour-lgbm{suffix}.txt", Body=booster.model_to_string().encode())
        s3.put_object(Bucket=BUCKET, Key=f"models/features{suffix}.json", Body=json.dumps(meta).encode())
    until = obs_until if obs_until is not None else day + pd.Timedelta(hours=5)
    recent = hourly[(hourly["hour_ist"] >= day - pd.Timedelta(days=2)) & (hourly["hour_ist"] < until)]
    for r in recent.itertuples():
        store.put_obs(int(r.location_id), r.hour_ist.tz_convert("UTC").isoformat(), float(r.pm25), 4)
    monkeypatch.setattr(forecast, "_s3", s3)
    monkeypatch.setattr(forecast, "_MODELS", {})
    monkeypatch.setattr(forecast, "stations", lambda: STATIONS)
    monkeypatch.setattr(forecast.meteo, "fetch", lambda *a, **k: met)
    return forecast


def test_forecast_decide_send_reply(aws, monkeypatch):
    from clearhour.handlers import decide, inbound, send

    day = pd.Timestamp("2025-11-13", tz="Asia/Kolkata")
    forecast = _seed_model_and_obs(monkeypatch, day)
    out = forecast.handler({"source": "live", "as_of": "2025-11-13T05:30:00+05:30"}, None)
    assert out["stations"] == 3 and out["forecast_key"] == "runs/2025-11-13/live/forecast.json"
    fc = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=out["forecast_key"])["Body"].read())
    assert set(fc["latest"]) == set(fc["stations"])  # every forecast station carries its latest reading
    assert fc["model"] == "fresh" and fc["blackout_h"] == 0

    store.table().put_item(
        Item={"pk": "SCHOOL#node/1", "sk": "PROFILE", "name": "सर्वोदय विद्यालय", "phone": "919999999999", "lang": "hi"}
    )
    monkeypatch.setattr(decide, "_s3", boto3.client("s3"))
    monkeypatch.setattr(decide, "schools", lambda: SCHOOLS)
    res = decide.handler(out, None)
    assert res["schools"] == 1  # the far school has no station forecast
    decisions = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=res["decisions_key"])["Body"].read())
    assert decisions["node/1"]["latest"] is not None
    live = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key="site/data/live.json")["Body"].read())
    assert [row[0] for row in live["schools"]] == ["node/1"] and len(live["stations"]) == 3
    assert res["alerts"] == [{"school_id": "node/1", "day": "2025-11-13"}]
    alert = store.get_alert("node/1", "2025-11-13")
    assert alert["status"] == "pending" and alert["params"][1] == "गुरु 13 नवंबर"

    monkeypatch.setenv("WA_SEND_AS", "text")  # the 24-hour-window fallback sends the same words as plain text
    sent = send.handler(res["alerts"][0], None)
    assert sent == {"school_id": "node/1", "status": "sent", "message_id": "dry-run"}
    alerts = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key="site/data/alerts.json")["Body"].read())
    assert alerts["days"]["2025-11-13"][0]["status"] == "sent"
    assert send.handler(res["alerts"][0], None)["status"] == "skipped"  # never twice
    assert decide.handler(out, None)["alerts"] == []  # a re-run doesn't queue it again

    today = datetime.now(IST).date().isoformat()
    store.put_alert_if_new("node/1", today, {"params": ["a", "b", "c", "d"]})  # queued, not sent yet
    entry = {
        "changes": [
            {"value": {"messages": [{"from": "919999999999", "id": "w", "type": "text", "text": {"body": "१"}}]}}
        ]
    }
    event = {"Records": [{"Sns": {"Message": json.dumps({"whatsAppWebhookEntry": json.dumps(entry)})}}]}
    assert inbound.handler(event, None) == {"handled": 1}
    assert store.get_alert("node/1", "2025-11-13")["acted"] is True  # the reply answers the alert it got, not today's
    assert "acted" not in store.get_alert("node/1", today)
    alerts = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key="site/data/alerts.json")["Body"].read())
    assert alerts["days"]["2025-11-13"][0]["acted"] is True


def test_stale_readings_still_forecast_but_leave_assembly_to_the_forecast(aws, monkeypatch):
    from clearhour.handlers import decide

    day = pd.Timestamp("2025-11-13", tz="Asia/Kolkata")
    forecast = _seed_model_and_obs(monkeypatch, day, obs_until=day - pd.Timedelta(hours=12))  # newest: 11:00 yesterday
    out = forecast.handler({"source": "live", "as_of": "2025-11-13T05:30:00+05:30"}, None)
    fc = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=out["forecast_key"])["Body"].read())
    assert fc["blackout_h"] == 17 and fc["obs_through"].startswith("2025-11-12T11:00") and fc["model"] == "stale"
    assert fc["latest"] == {}  # nothing from the night, so no reading drives the assembly call
    monkeypatch.setattr(decide, "_s3", boto3.client("s3"))
    monkeypatch.setattr(decide, "schools", lambda: SCHOOLS)
    res = decide.handler(out, None)
    assert res["schools"] == 1
    decisions = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=res["decisions_key"])["Body"].read())
    if decisions["node/1"]["kind"] == "clear_hour":
        assert decisions["node/1"]["clear_hour"] == 13  # stale mornings name 1 PM
