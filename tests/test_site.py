"""Dashboard files on mocked AWS (moto): the day's calls, the alert status, and the Web function's routes."""

import base64
import gzip
import json

import boto3
import pytest
from moto import mock_aws

from clearhour import site, store
from clearhour.handlers import web

BUCKET, TABLE = "clearhour-site-test", "clearhour-site-table"
STATIONS = [{"location_id": 100, "pm25_sensor_id": 1000, "name": "R K Puram", "lat": 28.56, "lon": 77.18}]
SCHOOLS = [{"id": "node/1", "name": "Kendriya Vidyalaya", "lat": 28.56, "lon": 77.17, "near": [[100, 1.0]]}]


@pytest.fixture
def aws(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": "ap-south-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "TABLE_NAME": TABLE,
        "DATA_BUCKET": BUCKET,
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
        monkeypatch.setattr(web, "_S3", None)
        monkeypatch.setattr(site, "stations", lambda: STATIONS)
        yield


def _read(key: str) -> str:
    return boto3.client("s3").get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()


def _event(path: str, method: str = "GET", gzip_ok: bool = True) -> dict:
    headers = {"accept-encoding": "gzip, br"} if gzip_ok else {}
    return {"rawPath": path, "headers": headers, "requestContext": {"http": {"method": method}}}


def test_day_file_has_compact_rows_and_station_readings(aws):
    fc = {
        "day": "2025-11-13",
        "source": "archive",
        "generated_at": "2026-10-08T12:00:00+05:30",
        "median_lead_h": 6.5,
        "latest": {"100": 238.0},
    }
    decision = {
        "kind": "clear_hour",
        "clear_hour": 13,
        "assembly_indoors": True,
        "limit_outdoor": True,
        "latest": 238.0,
        "hourly": {str(h): float(300 - 20 * (h - 8)) for h in range(8, 14)},
    }
    assert site.publish_day(fc, {"node/1": decision}, SCHOOLS) == "replay.json"
    body = json.loads(_read("site/data/replay.json"))
    assert body["stations"] == [[100, "R K Puram", 28.56, 77.18, 238.0]]
    row = ["node/1", "Kendriya Vidyalaya", 28.56, 77.17, "clear_hour", 13, 1, 1, 238.0]
    assert body["schools"] == [[*row, [300.0, 280.0, 260.0, 240.0, 220.0, 200.0], [100]]]


def test_alerts_file_tracks_sent_and_acted_without_phone_numbers(aws):
    store.table().put_item(
        Item={"pk": "SCHOOL#node/1", "sk": "PROFILE", "name": "KV RK Puram", "phone": "919999990123", "lang": "hi"}
    )
    attrs = {"params": ["a", "b", "c", "d"], "kind": "clear_hour", "clear_hour": 13, "assembly_indoors": True}
    store.put_alert_if_new("node/1", "2026-10-09", attrs)
    store.move_alert("node/1", "2026-10-09", "pending", "sent", sent_at="2026-10-09T00:04:02+00:00")
    store.mark_acted("node/1", "2026-10-09", "2026-10-09T01:11:30+00:00")
    assert site.publish_alerts() == 1
    raw = _read("site/data/alerts.json")
    assert "919999990123" not in raw
    entry = json.loads(raw)["days"]["2026-10-09"][0]
    assert entry["status"] == "sent" and entry["acted"] is True
    assert entry["clear_hour"] == 13 and entry["lang"] == "hi" and entry["params"] == ["a", "b", "c", "d"]


def test_web_serves_known_paths_gzipped_and_nothing_else(aws):
    boto3.client("s3").put_object(Bucket=BUCKET, Key="site/index.html", Body=b"<!doctype html>" + b" " * 2000)
    page = web.handler(_event("/"), None)
    assert page["statusCode"] == 200 and page["headers"]["Content-Encoding"] == "gzip"
    assert gzip.decompress(base64.b64decode(page["body"])).startswith(b"<!doctype html>")
    plain = web.handler(_event("/", gzip_ok=False), None)
    assert "Content-Encoding" not in plain["headers"]
    assert web.handler(_event("/data/live.json"), None)["statusCode"] == 404  # not published yet
    assert web.handler(_event("/../template.yaml"), None)["statusCode"] == 404
    assert web.handler(_event("/", "POST"), None)["statusCode"] == 405
