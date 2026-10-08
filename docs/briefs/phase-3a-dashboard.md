# Phase 3a brief: the dashboard, on AWS

Read `CLAUDE.md` first. Do the tasks in order: check, commit, then move to the next. Stop after P3-T2 and
report with the block at the end. The deploy in P3-T2 is approved, with `WA_MODE` still `dry_run`.

## What we're building

A public page for the video and the judges. It shows every Delhi school's call for the morning on a map:
- **Today / Replay:** today's live run, or the 13 Nov 2025 replay.
- **Language:** English or Hindi.
- **Demo schools:** each one's WhatsApp status, which flips to "Acted" within about 20 seconds of the "1"
  reply. That's the video's 0:40–1:20 moment.
- **School card:** click any school to see the exact message its principal would get, and a six-bar chart of
  its school hours with the Clear Hour in blue.

How it runs:
- **Data files:** the Lambdas write small JSON files to the data bucket under `site/data/`.
- **Web function:** a Lambda function URL serves the page and those files.
  - No CloudFront: brand-new accounts can be blocked from creating distributions until AWS verifies them.
  - No Amplify: that would mean a second hosting setup outside the template.
- **Basemap:** Amazon Location Service's Monochrome style, with an API key limited to map tiles and to the
  page's own address. If the key can't be made, the page falls back to OpenFreeMap tiles on its own.

The data files the page reads (`src/clearhour/site.py` writes them; no phone number ever goes in):
- `live.json`, `replay.json`: `day`, `source`, `generated_at`, `median_lead_h`, `stations`, and `schools`.
  - `stations` rows: `[location_id, name, lat, lon, latest]`.
  - `schools` rows: `[id, name, lat, lon, kind, clear_hour, assembly_indoors, limit_outdoor, latest,
    [PM2.5 at 08..13], [station ids]]`.
- `alerts.json`: `{"days": {"2026-10-09": [...], "2025-11-13-replay": [...]}}`. Each entry is one demo school's
  alert: status, sent and acted times, and the four params it was sent with.
- `config.json`: the map style URL, written by `scripts/publish_site.sh`.

**The page is Verbatim, design included.** I rendered it headlessly at desktop and phone widths, in both
languages, with 1,110 sample schools. If something looks wrong on the real deploy, send me a screenshot
rather than restyling it.

---

## P3-T1: Data files and the Web function

1. Save the patch below to `/tmp/dash.patch`. From the repo root, run `git apply --check /tmp/dash.patch`,
   then `git apply`. If a hunk fails, make the same change by hand. What it changes:
   - Decide publishes the day's file and `alerts.json`.
   - Send and Inbound refresh `alerts.json` after a send and after a "1". A failure there is logged and never
     breaks a send or a reply.
   - The template gains `WebFunction` with a public function URL, the `SiteUrl` output, and S3 write access for
     Send and Inbound.
   - `tests/test_pipeline.py` checks the dashboard files end to end.
2. Add the three new files below, all Verbatim:
   - `src/clearhour/site.py`
   - `src/clearhour/handlers/web.py`
   - `tests/test_site.py`
3. Add to CLAUDE.md:
   - `site/index.html` is the dashboard.
   - `scripts/publish_site.sh` uploads it; re-run it after any edit to the page.
   - The data files under `site/data/` never hold phone numbers.
4. Log in DECISIONS.md: the function URL instead of CloudFront or Amplify, and the Amazon Location key with
   its OpenFreeMap fallback (both explained above).

**Check:** `uv run pytest -q` (22 tests), ruff clean, cfn-lint clean, `sam build` ok.
Commit: `dashboard: data files and the Web function`

### `/tmp/dash.patch` (Verbatim)

```diff
--- a/src/clearhour/handlers/decide.py
+++ b/src/clearhour/handlers/decide.py
@@ -14,7 +14,7 @@
 import boto3
 
 from clearhour import decide as rules
-from clearhour import store
+from clearhour import site, store
 from clearhour.constants import TARGET_HOURS
 
 _s3 = boto3.client("s3")
@@ -54,7 +54,8 @@
     day, replay = date.fromisoformat(fc["day"]), fc["source"] != "live"
 
     decisions = {}
-    for sch in schools():
+    all_schools = schools()
+    for sch in all_schools:
         hourly = school_hourly(sch, fc["stations"])
         if hourly:
             now = school_latest(sch, fc.get("latest", {}))
@@ -88,5 +89,10 @@
         existing = None if created else store.get_alert(school_id, alert_day)
         if created or (existing and existing.get("status") == "pending"):
             alerts.append({"school_id": school_id, "day": alert_day})
+    try:  # the dashboard files; a failure here must not stop the alerts
+        site.publish_day(fc, decisions, all_schools)
+        site.publish_alerts()
+    except Exception as e:  # logged, never raised
+        print(json.dumps({"warning": "dashboard files not updated", "error": str(e)[:300]}))
     print(json.dumps({"day": fc["day"], "schools": len(decisions), "alerts": len(alerts)}))
     return {"day": fc["day"], "decisions_key": decisions_key, "schools": len(decisions), "alerts": alerts}
--- a/src/clearhour/handlers/inbound.py
+++ b/src/clearhour/handlers/inbound.py
@@ -9,7 +9,7 @@
 from datetime import UTC, datetime
 from zoneinfo import ZoneInfo
 
-from clearhour import store, whatsapp
+from clearhour import site, store, whatsapp
 
 IST = ZoneInfo("Asia/Kolkata")
 YES = {"1", "1.", "१", "done", "ok 1", "हो गया"}
@@ -44,6 +44,8 @@
             today = datetime.now(IST).date().isoformat()
             acted = store.mark_acted(school_id, today, datetime.now(UTC).isoformat(timespec="seconds"))
             body = text["thanks"].format(name=profile["name"]) if acted else text["no_alert"]
+            if acted:
+                site.publish_alerts_quietly()
         else:
             body = text["help"]
         whatsapp.send(whatsapp.text_payload(msg["from"], body))
--- a/src/clearhour/handlers/send.py
+++ b/src/clearhour/handlers/send.py
@@ -9,7 +9,7 @@
 import os
 from datetime import UTC, datetime
 
-from clearhour import store, whatsapp
+from clearhour import site, store, whatsapp
 
 
 def handler(event, context):
@@ -40,4 +40,5 @@
         message_id=message_id,
         sent_at=datetime.now(UTC).isoformat(timespec="seconds"),
     )
+    site.publish_alerts_quietly()
     return {"school_id": school_id, "status": "sent", "message_id": message_id}
--- a/template.yaml
+++ b/template.yaml
@@ -134,6 +134,20 @@
         - S3CrudPolicy:
             BucketName: !Ref DataBucket
 
+  WebFunction:
+    Type: AWS::Serverless::Function
+    Properties:
+      CodeUri: src/
+      Handler: clearhour.handlers.web.handler
+      Runtime: python3.13
+      MemorySize: 256
+      Timeout: 10
+      Policies:
+        - S3ReadPolicy:
+            BucketName: !Ref DataBucket
+      FunctionUrlConfig:
+        AuthType: NONE
+
   SendFunction:
     Type: AWS::Serverless::Function
     Properties:
@@ -153,6 +167,8 @@
       Policies:
         - DynamoDBCrudPolicy:
             TableName: !Ref ClearHourTable
+        - S3WritePolicy:
+            BucketName: !Ref DataBucket
         - Statement:
             - Effect: Allow
               Action: social-messaging:SendWhatsAppMessage
@@ -177,6 +193,8 @@
       Policies:
         - DynamoDBCrudPolicy:
             TableName: !Ref ClearHourTable
+        - S3WritePolicy:
+            BucketName: !Ref DataBucket
         - Statement:
             - Effect: Allow
               Action: social-messaging:SendWhatsAppMessage
@@ -226,6 +244,8 @@
             Input: '{"source": "live"}'
 
 Outputs:
+  SiteUrl:
+    Value: !GetAtt WebFunctionUrl.FunctionUrl
   TableName:
     Value: !Ref ClearHourTable
   DataBucketName:
--- a/tests/test_pipeline.py
+++ b/tests/test_pipeline.py
@@ -10,7 +10,7 @@
 from moto import mock_aws
 from synthetic import make_synthetic
 
-from clearhour import features, store
+from clearhour import features, site, store
 from clearhour.model import fit, with_station_category
 
 IST = ZoneInfo("Asia/Kolkata")
@@ -47,6 +47,8 @@
         )
         boto3.client("s3").create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
         monkeypatch.setattr(store, "_TABLE", None)
+        monkeypatch.setattr(site, "_S3", None)
+        monkeypatch.setattr(site, "stations", lambda: STATIONS)
         yield
 
 
@@ -97,12 +99,16 @@
     assert res["schools"] == 1  # the far school has no station forecast
     decisions = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=res["decisions_key"])["Body"].read())
     assert decisions["node/1"]["latest"] is not None
+    live = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key="site/data/live.json")["Body"].read())
+    assert [row[0] for row in live["schools"]] == ["node/1"] and len(live["stations"]) == 3
     assert res["alerts"] == [{"school_id": "node/1", "day": "2025-11-13"}]
     alert = store.get_alert("node/1", "2025-11-13")
     assert alert["status"] == "pending" and alert["params"][1] == "गुरु 13 नवंबर"
 
     sent = send.handler(res["alerts"][0], None)
     assert sent == {"school_id": "node/1", "status": "sent", "message_id": "dry-run"}
+    alerts = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key="site/data/alerts.json")["Body"].read())
+    assert alerts["days"]["2025-11-13"][0]["status"] == "sent"
     assert send.handler(res["alerts"][0], None)["status"] == "skipped"  # never twice
     assert decide.handler(out, None)["alerts"] == []  # a re-run doesn't queue it again
 
@@ -116,3 +122,5 @@
     event = {"Records": [{"Sns": {"Message": json.dumps({"whatsAppWebhookEntry": json.dumps(entry)})}}]}
     assert inbound.handler(event, None) == {"handled": 1}
     assert store.get_alert("node/1", today)["acted"] is True
+    alerts = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key="site/data/alerts.json")["Body"].read())
+    assert alerts["days"][today][0]["acted"] is True
```

### `src/clearhour/site.py` (Verbatim)

```python
"""What the dashboard reads, written to the data bucket under site/data/ and served by the Web function.

- live.json and replay.json: the latest live run and the latest replay. School rows are compact arrays.
- alerts.json: each demo school's recent alerts (status, sent and acted times, the params that were sent).

No phone numbers ever go into these files.
"""

from __future__ import annotations

import json
import os
from importlib import resources

import boto3
from boto3.dynamodb.conditions import Key

from clearhour import store
from clearhour.constants import TARGET_HOURS

PREFIX = "site/data/"
ALERTS_PER_SCHOOL = 14
_S3 = None


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.client("s3")
    return _S3


def stations() -> list[dict]:
    return json.loads(resources.files("clearhour").joinpath("stations.json").read_text())


def put_json(name: str, body: dict, max_age: int) -> None:
    s3().put_object(
        Bucket=os.environ["DATA_BUCKET"],
        Key=PREFIX + name,
        Body=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
        ContentType="application/json; charset=utf-8",
        CacheControl=f"max-age={max_age}",
    )


def school_row(school: dict, decision: dict) -> list:
    """[id, name, lat, lon, kind, clear_hour, assembly_indoors, limit_outdoor, latest, PM2.5 08..13, station ids]"""
    return [
        school["id"],
        school["name"],
        school["lat"],
        school["lon"],
        decision["kind"],
        decision["clear_hour"],
        int(decision["assembly_indoors"]),
        int(decision["limit_outdoor"]),
        decision.get("latest"),
        [decision["hourly"][str(h)] for h in TARGET_HOURS],
        [int(sid) for sid, _ in school["near"]],
    ]


def publish_day(fc: dict, decisions: dict, schools: list[dict]) -> str:
    """One run's calls for every school it covered: live.json for the 05:30 run, replay.json for a replay."""
    latest = fc.get("latest", {})
    body = {
        "v": 1,
        "day": fc["day"],
        "source": fc["source"],
        "generated_at": fc["generated_at"],
        "median_lead_h": fc.get("median_lead_h"),
        "hours": TARGET_HOURS,
        "stations": [
            [s["location_id"], s["name"], s["lat"], s["lon"], latest.get(str(s["location_id"]))] for s in stations()
        ],
        "schools": [school_row(s, decisions[s["id"]]) for s in schools if s["id"] in decisions],
    }
    name = "live.json" if fc["source"] == "live" else "replay.json"
    put_json(name, body, 60)
    return name


def recent_alerts(school_id: str) -> list[dict]:
    resp = store.table().query(
        KeyConditionExpression=Key("pk").eq(f"SCHOOL#{school_id}") & Key("sk").begins_with("ALERT#"),
        ScanIndexForward=False,
        Limit=ALERTS_PER_SCHOOL,
    )
    return resp["Items"]


def alert_entry(school_id: str, profile: dict, alert: dict) -> dict:
    clear = alert.get("clear_hour")
    return {
        "school_id": school_id,
        "name": profile["name"],
        "lang": profile.get("lang", "en"),
        "status": alert.get("status"),
        "sent_at": alert.get("sent_at"),
        "acted": bool(alert.get("acted")),
        "acted_at": alert.get("acted_at"),
        "kind": alert.get("kind"),
        "clear_hour": int(clear) if clear is not None else None,
        "params": [str(p) for p in alert.get("params", [])],
    }


def publish_alerts() -> int:
    """alerts.json: for each alert day ("2026-10-09", "2025-11-13-replay"), the demo schools' alerts."""
    days: dict[str, list[dict]] = {}
    for profile in store.profiles():
        school_id = profile["pk"].split("#", 1)[1]
        for alert in recent_alerts(school_id):
            day = alert["sk"].split("#", 1)[1]
            days.setdefault(day, []).append(alert_entry(school_id, profile, alert))
    for entries in days.values():
        entries.sort(key=lambda e: e["name"])
    put_json("alerts.json", {"days": days}, 10)
    return sum(len(v) for v in days.values())


def publish_alerts_quietly() -> None:
    """For the send and reply paths: the dashboard update must never fail a WhatsApp send or reply."""
    try:
        publish_alerts()
    except Exception as e:  # logged, never raised
        print(json.dumps({"warning": "alerts.json not updated", "error": str(e)[:300]}))
```

### `src/clearhour/handlers/web.py` (Verbatim)

```python
"""Web Lambda (function URL): the dashboard page and its data files, read from the data bucket's site/ prefix.

Only the paths below are served. JSON and HTML go out gzipped when the browser accepts it.
"""

from __future__ import annotations

import base64
import gzip
import os

import boto3
from botocore.exceptions import ClientError

ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8", 300),
    "/index.html": ("index.html", "text/html; charset=utf-8", 300),
    "/data/live.json": ("data/live.json", "application/json; charset=utf-8", 60),
    "/data/replay.json": ("data/replay.json", "application/json; charset=utf-8", 60),
    "/data/alerts.json": ("data/alerts.json", "application/json; charset=utf-8", 5),
    "/data/config.json": ("data/config.json", "application/json; charset=utf-8", 300),
}
_S3 = None


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.client("s3")
    return _S3


def _reply(status: int, text: str) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "text/plain; charset=utf-8"}, "body": text}


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if method not in ("GET", "HEAD"):
        return _reply(405, "Method not allowed")
    route = ROUTES.get(event.get("rawPath", "/"))
    if not route:
        return _reply(404, "Not found")
    key, content_type, max_age = route
    try:
        body = s3().get_object(Bucket=os.environ["DATA_BUCKET"], Key=f"site/{key}")["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return _reply(404, "Not published yet")
        raise
    headers = {
        "Content-Type": content_type,
        "Cache-Control": f"public, max-age={max_age}",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Vary": "Accept-Encoding",
    }
    if "gzip" in (event.get("headers") or {}).get("accept-encoding", "") and len(body) > 1024:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return {
        "statusCode": 200,
        "headers": headers,
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }
```

### `tests/test_site.py` (Verbatim)

```python
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
```

---

## P3-T2: The page, published

1. Add `site/index.html` (Verbatim, below) and `scripts/publish_site.sh` (Verbatim, below), then
   `chmod +x scripts/publish_site.sh`.
2. Make `scripts/publish_site.sh` the last line of `scripts/deploy.sh`.
3. Deploy with `./scripts/deploy.sh`. The script prints the dashboard address.
4. So both views have data, start two dry runs:
   - `{"source": "archive", "as_of": "2025-11-13", "resend": true}`
   - `{"source": "live", "resend": true}`

   If `WA_MODE` is no longer `dry_run` by then, these would send real messages. In that case, skip them and
   ask me first.
5. Check in a desktop browser:
   - The Monochrome map tiles load. If you see OpenFreeMap tiles instead, report the `publish_site.sh` output.
   - Today and Replay both work.
   - Clicking a demo school shows the same four params as that run's Send log.
   - The Hindi switch works.
   - There are no console errors other than font or tile warnings.
6. Check at phone width: the map is on top, the brief is below it, and the school card opens as a bottom
   sheet. Take one desktop screenshot and one phone screenshot of the replay with a demo school's card open.
7. The page caches for five minutes. After any later page change, re-run `scripts/publish_site.sh` and
   hard-refresh.

**Check:** the dashboard address loads in a signed-out browser.
Commit: `dashboard: page and publish script`. Then stop.

### `site/index.html` (Verbatim)

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClearHour · Delhi schools</title>
<meta name="description" content="The cleanest hour of the school day for every Delhi school, forecast each morning and sent to principals on WhatsApp.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Mukta:wght@400;500;600;700&display=swap" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
:root {
  color-scheme: light;
  --page: #eef2f4;
  --surface: #ffffff;
  --ink: #1e2b2f;
  --ink-2: #4f5f63;
  --muted: #7d8c92;
  --hairline: #d8e0e3;
  --clear: #2a78d6;
  --clear-wash: #e5effb;
  --lvl-0: #2a78d6;
  --lvl-1: #e9b949;
  --lvl-2: #e0702e;
  --lvl-3: #b52a3a;
  --lvl-4: #5a1640;
  --bar: #b9c6cb;
  --wa-bubble: #e7f6dc;
  --wa-ink: #1d2a1b;
  --radius-panel: 14px;
  --radius-chip: 999px;
  --font: "Mukta", system-ui, -apple-system, "Segoe UI", "Nirmala UI", sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--page);
  color: var(--ink);
  font: 400 15px/1.45 var(--font);
  -webkit-font-smoothing: antialiased;
}
button, input { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--clear); outline-offset: 2px; }

.app { display: grid; grid-template-rows: auto 1fr; height: 100vh; height: 100dvh; }
.bar {
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  padding: 10px 20px; background: var(--surface); border-bottom: 1px solid var(--hairline);
}
.mark { display: flex; align-items: center; gap: 10px; margin-right: auto; }
.mark svg { flex: none; }
.wordmark { font-weight: 700; font-size: 21px; letter-spacing: -0.01em; }
.wordmark span { font-weight: 400; color: var(--ink-2); }
.place { color: var(--ink-2); font-size: 14px; }
.seg-day { order: 2; }
.seg-lang { order: 3; }
.seg { display: inline-flex; background: var(--page); border-radius: var(--radius-chip); padding: 3px; }
.seg button {
  border: 0; background: transparent; padding: 4px 14px; border-radius: var(--radius-chip);
  cursor: pointer; color: var(--ink-2); font-weight: 500; white-space: nowrap;
}
.seg button[aria-pressed="true"] { background: var(--surface); color: var(--ink); box-shadow: 0 0 0 1px var(--hairline); }
.seg button:disabled { opacity: 0.45; cursor: default; }

.main { display: grid; grid-template-columns: minmax(330px, 400px) 1fr; min-height: 0; }
.brief { overflow-y: auto; padding: 22px 22px 28px; background: var(--surface); border-right: 1px solid var(--hairline); }
.mapwrap { position: relative; min-height: 0; }
#map { position: absolute; inset: 0; }

.replay-note {
  display: none; margin: 0 0 14px; padding: 8px 12px; border-radius: 10px;
  background: #fbf0dd; color: #6b4a0e; font-size: 14px;
}
.is-replay .replay-note { display: block; }
.dateline { color: var(--ink-2); font-size: 14px; margin: 0 0 4px; }
.headline { font-size: 25px; line-height: 1.22; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 18px; }
.headline .slot { color: var(--clear); white-space: nowrap; }

.strip-title { font-size: 13px; color: var(--ink-2); margin: 0 0 6px; }
.strip { width: 100%; height: auto; display: block; overflow: visible; }
.strip text { font-family: var(--font); }
.strip .v { font-size: 12px; fill: var(--ink-2); }
.strip .v.on { fill: var(--clear); font-weight: 700; }
.strip .t { font-size: 12px; fill: var(--muted); }
.strip .ref { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; }
.strip .reflabel { font-size: 11px; fill: var(--muted); }
.strip .base { stroke: var(--hairline); stroke-width: 1; }

.legend { list-style: none; margin: 20px 0 0; padding: 0; }
.legend li { display: grid; grid-template-columns: 18px 1fr auto; gap: 8px; align-items: baseline; padding: 5px 0; border-top: 1px solid var(--hairline); }
.legend li:first-child { border-top: 0; }
.legend .dot { width: 12px; height: 12px; border-radius: 50%; transform: translateY(1px); box-shadow: inset 0 0 0 1px rgba(0,0,0,0.18); }
.legend .n { font-variant-numeric: tabular-nums; color: var(--ink-2); }
.legend li.zero { color: var(--muted); }

.mapkey { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 12px 0 0; color: var(--ink-2); font-size: 13px; }
.mapkey .key { display: inline-flex; align-items: center; gap: 7px; }
.mapkey .k-mon, .mapkey .k-demo { border-radius: 50%; background: #fff; flex: none; }
.mapkey .k-mon { border: 2px solid var(--ink); width: 10px; height: 10px; }
.mapkey .k-demo { border: 3px solid var(--clear); width: 14px; height: 14px; }
.section-title { font-size: 15px; font-weight: 600; margin: 26px 0 8px; }
.pilots { list-style: none; margin: 0; padding: 0; }
.pilots li { border-top: 1px solid var(--hairline); }
.pilots button {
  width: 100%; text-align: left; border: 0; background: none; padding: 9px 0; cursor: pointer;
  display: grid; grid-template-columns: 1fr auto; gap: 2px 10px;
}
.pilots .pname { font-weight: 500; }
.pilots .pcall { color: var(--ink-2); font-size: 13px; grid-column: 1; }
.status { grid-row: 1 / span 2; grid-column: 2; align-self: center; font-size: 13px; padding: 2px 10px; border-radius: var(--radius-chip); background: var(--page); color: var(--ink-2); white-space: nowrap; }
.status.sent { background: var(--clear-wash); color: #1b4f8f; }
.status.acted { background: #ddf1e4; color: #145c2e; }
.empty { color: var(--muted); font-size: 14px; padding: 6px 0; }

.proof { margin-top: 26px; padding-top: 14px; border-top: 1px solid var(--hairline); color: var(--ink-2); font-size: 14px; }
.proof strong { color: var(--ink); }
.credits { margin-top: 14px; color: var(--muted); font-size: 12.5px; }
.credits a { color: inherit; }

.search { position: absolute; top: 14px; left: 14px; z-index: 2; width: min(320px, calc(100% - 28px)); }
.search input {
  width: 100%; padding: 9px 14px; border-radius: var(--radius-chip); border: 1px solid var(--hairline);
  background: var(--surface); box-shadow: 0 2px 10px rgba(30,43,47,0.08);
}

.card {
  position: absolute; z-index: 3; top: 14px; right: 14px; width: 360px; max-height: calc(100% - 28px);
  overflow-y: auto; background: var(--surface); border-radius: var(--radius-panel);
  box-shadow: 0 10px 32px rgba(30,43,47,0.18); padding: 18px 18px 16px; display: none;
}
.card.open { display: block; }
.card .close {
  position: absolute; top: 10px; right: 10px; width: 32px; height: 32px; border-radius: 50%;
  border: 0; background: var(--page); cursor: pointer; font-size: 18px; line-height: 1;
}
.card h2 { font-size: 19px; line-height: 1.25; margin: 0 40px 2px 0; }
.card .near { color: var(--ink-2); font-size: 13px; margin: 0 0 12px; }
.callchip { display: inline-flex; align-items: center; gap: 7px; font-size: 14px; font-weight: 500; margin-bottom: 12px; }
.callchip .dot { width: 11px; height: 11px; border-radius: 50%; }
.bubble {
  background: var(--wa-bubble); color: var(--wa-ink); border-radius: 12px 12px 12px 3px;
  padding: 10px 12px; font-size: 14.5px; line-height: 1.45; margin: 4px 0 14px;
}
.bubble .meta { display: block; text-align: right; font-size: 11.5px; color: #5d7259; margin-top: 4px; }
.card .label { font-size: 13px; color: var(--ink-2); margin: 0 0 4px; }
.card .latest { font-size: 14px; color: var(--ink-2); margin: 10px 0 0; }
.card .latest b { color: var(--ink); font-weight: 600; }
.card .demo { margin: 10px 0 0; font-size: 13px; color: var(--ink-2); }

.maplibregl-popup-content { font: 400 13.5px/1.35 var(--font); padding: 7px 10px; border-radius: 8px; }
.pilot-pin {
  width: 18px; height: 18px; border-radius: 50%; background: var(--surface);
  border: 3px solid var(--clear); box-shadow: 0 1px 4px rgba(0,0,0,0.3); cursor: pointer;
}
.pilot-pin.acted { background: var(--clear); }

@media (max-width: 900px) {
  .app { height: auto; min-height: 100vh; }
  .bar { padding: 10px 16px; gap: 10px; }
  .place { display: none; }
  .seg-lang { order: 1; }
  .seg-day { order: 2; width: 100%; }
  .seg-day button { flex: 1; }
  .main { grid-template-columns: 1fr; }
  .mapwrap { height: 58vh; order: -1; }
  .brief { border-right: 0; padding: 18px 16px 28px; }
  .headline { font-size: 22px; }
  .card {
    position: fixed; top: auto; left: 0; right: 0; bottom: 0; width: auto; max-height: 72vh;
    border-radius: var(--radius-panel) var(--radius-panel) 0 0;
  }
}
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
</style>
</head>
<body>
<div class="app" id="app">
  <header class="bar">
    <div class="mark">
      <svg width="30" height="30" viewBox="0 0 30 30" aria-hidden="true">
        <circle cx="15" cy="15" r="9" fill="var(--clear)"/>
        <path d="M3 19h9M18 19h9M6 23.5h18" stroke="#aab7bc" stroke-width="2.2" stroke-linecap="round"/>
      </svg>
      <div class="wordmark">ClearHour <span class="place" data-t="place">Delhi schools</span></div>
    </div>
    <div class="seg seg-day" role="group" aria-label="Day">
      <button type="button" id="btn-live" aria-pressed="true" data-t="today">Today</button>
      <button type="button" id="btn-replay" aria-pressed="false">Replay</button>
    </div>
    <div class="seg seg-lang" role="group" aria-label="Language">
      <button type="button" id="btn-en" aria-pressed="true" lang="en">EN</button>
      <button type="button" id="btn-hi" aria-pressed="false" lang="hi">हिं</button>
    </div>
  </header>
  <div class="main">
    <aside class="brief">
      <p class="replay-note" id="replay-note"></p>
      <p class="dateline" id="dateline"></p>
      <h1 class="headline" id="headline"></h1>
      <p class="strip-title" id="city-strip-title"></p>
      <svg class="strip" id="city-strip" role="img"></svg>
      <ul class="legend" id="legend"></ul>
      <p class="mapkey"><span class="key"><span class="k-mon"></span><span id="key-monitor"></span></span><span class="key"><span class="k-demo"></span><span id="key-demo"></span></span></p>
      <h2 class="section-title" id="pilots-title"></h2>
      <ul class="pilots" id="pilots" aria-live="polite"></ul>
      <p class="proof" id="proof"></p>
      <p class="credits" id="credits"></p>
    </aside>
    <div class="mapwrap">
      <div id="map" role="region" aria-label="Map of schools"></div>
      <div class="search">
        <input id="search" list="school-names" autocomplete="off" aria-label="Find a school">
        <datalist id="school-names"></datalist>
      </div>
      <section class="card" id="card" aria-live="polite">
        <button type="button" class="close" id="card-close" aria-label="Close">×</button>
        <div id="card-body"></div>
      </section>
    </div>
  </div>
</div>
<script>
"use strict";
// Backtest numbers shown on the page (outputs/backtest.json, the production rule's row).
const PROOF = { cut: 25, pick: 151, assembly: 202, days: "1,956", monitors: 46 };
const HOURS = [8, 9, 10, 11, 12, 13];
const FALLBACK_STYLE = "https://tiles.openfreemap.org/styles/positron";
const BLANK_STYLE = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#e9eef0" } }] };
const LEVEL_COLORS = ["#2a78d6", "#e9b949", "#e0702e", "#b52a3a", "#5a1640"];
const LEVEL_RINGS = ["#1c5cab", "#9a7412", "#9a4313", "#7a1a26", "#3a0d29"];

const T = {
  en: {
    place: "Delhi schools", today: "Today", replay: (d) => `Replay ${d}`,
    replayNote: (d) => `Replay of ${d}, run on that morning's data. This is not today's air.`,
    forecastFor: (d, t) => `Forecast for ${d}, made at ${t}`,
    none: "No forecast yet. The first run is at 5:30 AM IST.",
    allFine: (n) => `Air is fine at all ${n} schools today.`,
    fine: (k, n) => `Air is fine at ${k} of ${n} schools today.`,
    indoors: (k, n) => (k === n ? `Hold assembly indoors at all ${n} schools.` : `Hold assembly indoors at ${k} of ${n} schools.`),
    outdoors: (n) => `Assembly can stay outside at all ${n} schools.`,
    mostSlot: (s) => ` For most, the cleanest hour is <span class="slot">${s}</span>.`,
    noWindow: (k) => ` ${k} schools have no safe window.`,
    stripCity: "Typical school, forecast PM2.5 by hour (µg/m³)",
    stripSchool: "Forecast PM2.5 by school hour (µg/m³)",
    refPoor: "very poor", refSevere: "severe",
    levels: ["Air fine all day", "Clear Hour; assembly can stay outside", "Clear Hour; assembly indoors",
             "Even the Clear Hour is very poor", "No safe window"],
    pilotsTitle: "Demo schools", noPilots: "No demo schools yet.",
    keyMonitor: "Air monitor", keyDemo: "Demo school (gets the WhatsApp alert)",
    status: { none: "No alert", pending: "Queued", sending: "Sending", sent: "Sent", acted: "Acted" },
    proof: `Tested on winter 2025–26: at the Clear Hour, PM2.5 was <strong>${PROOF.cut}% lower</strong> than at 8 AM assembly (${PROOF.pick} vs ${PROOF.assembly} µg/m³), across ${PROOF.days} school days at ${PROOF.monitors} monitors.`,
    credits: 'Readings: CPCB and DPCC monitors via <a href="https://openaq.org">OpenAQ</a>. Weather and CAMS: <a href="https://open-meteo.com">Open-Meteo</a>. Schools: <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>.',
    search: "Find a school", message: "Message to the principal", near: "Monitors used",
    latest: (v) => `Latest monitor reading, before 5 AM: <b>${v} µg/m³</b>`,
    demo: "Demo school: alerts go to the builder's own phone.",
    sentAt: (t) => `Sent ${t}`, actedAt: (t) => `Acted on at ${t}`, waiting: "Waiting for the reply “1”",
    msg: (p) => `ClearHour air update for ${p[0]} on ${p[1]}: ${p[2]} Cleanest hour for outdoor activity: ${p[3]}. Reply 1 once you have moved outdoor activities.`,
    adv: { fine: "Air is fine for outdoor activity today.", no_window: "No safe window today. Keep assembly, PE and recess indoors.",
           assembly_in: "Hold assembly indoors.", assembly_ok: "Assembly can stay outdoors.", keep_short: "Keep outdoor time short.",
           any_time: "any time", none_today: "none today", replay: " (replay)" },
    dow: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    mon: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
  },
  hi: {
    place: "दिल्ली के स्कूल", today: "आज", replay: (d) => `रीप्ले ${d}`,
    replayNote: (d) => `${d} की सुबह का रीप्ले, उसी सुबह के डेटा से। यह आज की हवा नहीं है।`,
    forecastFor: (d, t) => `${d} का पूर्वानुमान, ${t} पर बना`,
    none: "अभी कोई पूर्वानुमान नहीं है। पहला रन सुबह 5:30 बजे होगा।",
    allFine: (n) => `आज सभी ${n} स्कूलों में हवा ठीक है।`,
    fine: (k, n) => `आज ${n} में से ${k} स्कूलों में हवा ठीक है।`,
    indoors: (k, n) => (k === n ? `सभी ${n} स्कूलों में प्रार्थना सभा अंदर करें।` : `${n} में से ${k} स्कूलों में प्रार्थना सभा अंदर करें।`),
    outdoors: (n) => `सभी ${n} स्कूलों में प्रार्थना सभा बाहर हो सकती है।`,
    mostSlot: (s) => ` ज़्यादातर के लिए सबसे साफ़ समय <span class="slot">${s}</span> है।`,
    noWindow: (k) => ` ${k} स्कूलों में आज कोई सुरक्षित समय नहीं है।`,
    stripCity: "आम स्कूल, हर घंटे का अनुमानित PM2.5 (µg/m³)",
    stripSchool: "स्कूल के हर घंटे का अनुमानित PM2.5 (µg/m³)",
    refPoor: "बहुत ख़राब", refSevere: "गंभीर",
    levels: ["पूरे दिन हवा ठीक", "साफ़ समय; प्रार्थना सभा बाहर हो सकती है", "साफ़ समय; प्रार्थना सभा अंदर",
             "सबसे साफ़ समय भी बहुत ख़राब", "कोई सुरक्षित समय नहीं"],
    pilotsTitle: "डेमो स्कूल", noPilots: "अभी कोई डेमो स्कूल नहीं।",
    keyMonitor: "हवा मॉनिटर", keyDemo: "डेमो स्कूल (WhatsApp सूचना पाता है)",
    status: { none: "कोई सूचना नहीं", pending: "कतार में", sending: "भेजी जा रही", sent: "भेजी गई", acted: "कार्रवाई हुई" },
    proof: `2025–26 की सर्दियों पर परखा गया: साफ़ समय में PM2.5 सुबह 8 बजे की प्रार्थना सभा से <strong>${PROOF.cut}% कम</strong> था (${PROOF.pick} बनाम ${PROOF.assembly} µg/m³), ${PROOF.monitors} मॉनिटरों पर ${PROOF.days} स्कूली दिनों में।`,
    credits: 'रीडिंग: CPCB और DPCC मॉनिटर, <a href="https://openaq.org">OpenAQ</a> से। मौसम और CAMS: <a href="https://open-meteo.com">Open-Meteo</a>। स्कूल: <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>।',
    search: "स्कूल खोजें", message: "प्रधानाचार्य को संदेश", near: "इस्तेमाल हुए मॉनिटर",
    latest: (v) => `सुबह 5 बजे से पहले की ताज़ा रीडिंग: <b>${v} µg/m³</b>`,
    demo: "डेमो स्कूल: सूचनाएँ बनाने वाले के अपने फ़ोन पर जाती हैं।",
    sentAt: (t) => `${t} पर भेजी गई`, actedAt: (t) => `${t} पर कार्रवाई हुई`, waiting: "जवाब “1” का इंतज़ार",
    msg: (p) => `ClearHour वायु सूचना – ${p[0]}, ${p[1]}: ${p[2]} बाहरी गतिविधियों के लिए सबसे साफ़ समय: ${p[3]}। गतिविधियाँ बदलने के बाद 1 लिखकर भेजें।`,
    adv: { fine: "आज बाहरी गतिविधियों के लिए हवा ठीक है।", no_window: "आज कोई सुरक्षित समय नहीं है। प्रार्थना सभा, पीटी और खेल अंदर ही कराएँ।",
           assembly_in: "प्रार्थना सभा अंदर करें।", assembly_ok: "प्रार्थना सभा बाहर हो सकती है।", keep_short: "बच्चों को बाहर कम समय ही रखें।",
           any_time: "किसी भी समय", none_today: "आज कोई नहीं", replay: " (रीप्ले)" },
    dow: ["सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि", "रवि"],
    mon: ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"],
  },
};

const state = { lang: "en", mode: "live", data: {}, alerts: { days: {} }, map: null, markers: [], selected: null };
const $ = (id) => document.getElementById(id);
const t = () => T[state.lang];
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Mirrors clearhour/decide.py so the preview matches what the principal receives.
function clock(h) { return [(h % 12) || 12, h % 24 < 12 ? "AM" : "PM"]; }
function slotLabel(h, lang) {
  const [a, ma] = clock(h), [b, mb] = clock(h + 1);
  if (lang === "hi") return `${h < 12 ? "सुबह" : h < 16 ? "दोपहर" : "शाम"} ${a}:00–${b}:00`;
  return ma === mb ? `${a}:00–${b}:00 ${ma}` : `${a}:00 ${ma}–${b}:00 ${mb}`;
}
function dayLabel(iso, lang) {
  const d = new Date(iso + "T00:00:00Z"), L = T[lang];
  return `${L.dow[(d.getUTCDay() + 6) % 7]} ${d.getUTCDate()} ${L.mon[d.getUTCMonth()]}`;
}
function longDay(iso, lang) { return `${dayLabel(iso, lang)} ${iso.slice(0, 4)}`; }
function messageParams(s, name, iso, lang, replay) {
  const A = T[lang].adv;
  const when = dayLabel(iso, lang) + (replay ? A.replay : "");
  if (s.kind === "fine") return [name, when, A.fine, A.any_time];
  if (s.kind === "no_window") return [name, when, A.no_window, A.none_today];
  let advice = s.indoors ? A.assembly_in : A.assembly_ok;
  if (s.short) advice = `${advice} ${A.keep_short}`;
  return [name, when, advice, slotLabel(s.clear, lang)];
}

function level(s) {
  if (s.kind === "fine") return 0;
  if (s.kind === "no_window") return 4;
  if (s.short) return 3;
  return s.indoors ? 2 : 1;
}
function unpack(row) {
  const [id, name, lat, lon, kind, clear, indoors, short, latest, hourly, near] = row;
  const s = { id, name, lat, lon, kind, clear, indoors: !!indoors, short: !!short, latest, hourly, near: near || [] };
  s.level = level(s);
  return s;
}
function current() { return state.data[state.mode]; }
function alertDay() { const d = current(); return d ? d.day + (state.mode === "replay" ? "-replay" : "") : null; }
function alertsFor(id) { return (state.alerts.days[alertDay()] || []).find((a) => a.school_id === id); }
function median(xs) { const v = xs.filter((x) => x != null).sort((a, b) => a - b); if (!v.length) return null; const m = v.length >> 1; return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2; }
function timeIST(iso) { if (!iso) return ""; return new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "numeric", minute: "2-digit" }); }

// The school-day strip: six bars, the Clear Hour in blue, dashed lines at 120 and 250 µg/m³ labelled in a gutter.
function drawStrip(svg, values, clearHour) {
  const W = 340, H = 136, top = 18, bottom = 22, left = 2, gutter = 62;
  const vals = values.map((v) => (v == null ? 0 : v));
  const maxV = Math.max(140, ...vals) * 1.1;
  const y = (v) => top + (H - top - bottom) * (1 - v / maxV);
  const x0 = left, x1 = W - gutter, bw = (x1 - x0) / HOURS.length, gap = 6;
  let out = `<line class="base" x1="${x0}" x2="${x1}" y1="${y(0)}" y2="${y(0)}"/>`;
  for (const [ref, label] of [[120, t().refPoor], [250, t().refSevere]]) {
    if (ref >= maxV) continue;
    out += `<line class="ref" x1="${x0}" x2="${x1 + 4}" y1="${y(ref)}" y2="${y(ref)}"/>`;
    out += `<text class="reflabel" x="${x1 + 8}" y="${y(ref) + 1}">${ref}</text><text class="reflabel" x="${x1 + 8}" y="${y(ref) + 12}">${esc(label)}</text>`;
  }
  HOURS.forEach((h, i) => {
    const v = values[i], x = x0 + i * bw + gap / 2, w = bw - gap, on = h === clearHour;
    const yy = v == null ? y(0) : y(v), r = Math.min(4, w / 2);
    if (v != null) out += `<path d="M${x},${y(0)} V${yy + r} Q${x},${yy} ${x + r},${yy} H${x + w - r} Q${x + w},${yy} ${x + w},${yy + r} V${y(0)} Z" fill="${on ? "var(--clear)" : "var(--bar)"}"><title>${slotLabel(h, state.lang)}: ${Math.round(v)} µg/m³</title></path>`;
    out += `<text class="v${on ? " on" : ""}" x="${x + w / 2}" y="${yy - 5}" text-anchor="middle">${v == null ? "–" : Math.round(v)}</text>`;
    const [hh, ap] = clock(h);
    const lab = i === 0 || i === HOURS.length - 1 ? `${hh} ${state.lang === "hi" ? (h < 12 ? "सुबह" : "दोपहर") : ap}` : `${hh}`;
    out += `<text class="t" x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${lab}</text>`;
  });
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = out;
}

function renderBrief() {
  const L = t(), d = current();
  document.documentElement.lang = state.lang;
  $("app").classList.toggle("is-replay", state.mode === "replay");
  document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = L[el.dataset.t]; });
  $("btn-replay").textContent = state.data.replay ? L.replay(longDay(state.data.replay.day, state.lang)) : L.replay("");
  $("search").placeholder = L.search;
  $("proof").innerHTML = L.proof;
  $("credits").innerHTML = L.credits;
  $("pilots-title").textContent = L.pilotsTitle;
  $("key-monitor").textContent = L.keyMonitor;
  $("key-demo").textContent = L.keyDemo;
  if (!d) {
    $("dateline").textContent = ""; $("headline").textContent = L.none;
    $("city-strip").innerHTML = ""; $("city-strip-title").textContent = ""; $("legend").innerHTML = ""; $("pilots").innerHTML = "";
    return;
  }
  $("replay-note").textContent = L.replayNote(longDay(d.day, state.lang));
  $("dateline").textContent = L.forecastFor(longDay(d.day, state.lang), timeIST(d.generated_at));
  const n = d.schools.length;
  const counts = [0, 0, 0, 0, 0];
  const clearCounts = {};
  d.schools.forEach((s) => { counts[s.level]++; if (s.clear != null) clearCounts[s.clear] = (clearCounts[s.clear] || 0) + 1; });
  const indoors = d.schools.filter((s) => s.indoors).length;
  const topSlot = Object.entries(clearCounts).sort((a, b) => b[1] - a[1])[0];
  let h;
  if (counts[0] === n) h = L.allFine(n.toLocaleString("en-IN"));
  else {
    h = indoors ? L.indoors(indoors.toLocaleString("en-IN"), n.toLocaleString("en-IN")) : (counts[0] ? L.fine(counts[0].toLocaleString("en-IN"), n.toLocaleString("en-IN")) : L.outdoors(n.toLocaleString("en-IN")));
    if (topSlot) h += L.mostSlot(slotLabel(+topSlot[0], state.lang));
    if (counts[4]) h += L.noWindow(counts[4].toLocaleString("en-IN"));
  }
  $("headline").innerHTML = h;
  $("city-strip-title").textContent = L.stripCity;
  const med = HOURS.map((_, i) => median(d.schools.map((s) => s.hourly[i])));
  drawStrip($("city-strip"), med, topSlot ? +topSlot[0] : null);
  $("city-strip").setAttribute("aria-label", L.stripCity);
  $("legend").innerHTML = L.levels.map((label, i) => `<li class="${counts[i] ? "" : "zero"}"><span class="dot" style="background:${LEVEL_COLORS[i]}"></span><span>${esc(label)}</span><span class="n">${counts[i].toLocaleString("en-IN")}</span></li>`).join("");
  renderPilots();
}

function pilotStatus(a) {
  if (!a) return ["none", t().status.none];
  if (a.acted) return ["acted", t().status.acted];
  return [a.status === "sent" ? "sent" : "", t().status[a.status] || a.status];
}
function renderPilots() {
  const d = current();
  const list = state.alerts.days[alertDay()] || [];
  const byId = new Map(d ? d.schools.map((s) => [s.id, s]) : []);
  if (!list.length) { $("pilots").innerHTML = `<li class="empty">${esc(t().noPilots)}</li>`; }
  else {
    $("pilots").innerHTML = list.map((a) => {
      const s = byId.get(a.school_id);
      const [cls, label] = pilotStatus(a);
      const call = s ? t().levels[s.level] : "";
      return `<li><button type="button" data-id="${esc(a.school_id)}"><span class="pname">${esc(a.name)}</span><span class="pcall">${esc(call)}</span><span class="status ${cls}">${esc(label)}</span></button></li>`;
    }).join("");
    $("pilots").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => select(b.dataset.id, true)));
  }
  drawPilotPins();
}

function drawPilotPins() {
  state.markers.forEach((m) => m.remove());
  state.markers = [];
  const d = current();
  if (!state.map || !d) return;
  const byId = new Map(d.schools.map((s) => [s.id, s]));
  for (const a of state.alerts.days[alertDay()] || []) {
    const s = byId.get(a.school_id);
    if (!s) continue;
    const el = document.createElement("button");
    el.type = "button";
    el.className = "pilot-pin" + (a.acted ? " acted" : "");
    el.setAttribute("aria-label", a.name);
    el.addEventListener("click", (e) => { e.stopPropagation(); select(s.id, false); });
    state.markers.push(new maplibregl.Marker({ element: el }).setLngLat([s.lon, s.lat]).addTo(state.map));
  }
}

function renderCard() {
  const d = current(), id = state.selected;
  const s = d && id ? d.schools.find((x) => x.id === id) : null;
  if (!s) { $("card").classList.remove("open"); return; }
  const L = t(), a = alertsFor(s.id), replay = state.mode === "replay";
  const name = a ? a.name : s.name;
  const params = a && a.params && a.lang === state.lang ? a.params : messageParams(s, name, d.day, state.lang, replay);
  const stationNames = new Map((d.stations || []).map(([sid, sname]) => [sid, sname]));
  const near = s.near.map((sid) => stationNames.get(sid)).filter(Boolean);
  let meta = "";
  if (a) meta = a.acted ? L.actedAt(timeIST(a.acted_at)) : a.sent_at ? `${L.sentAt(timeIST(a.sent_at))} · ${L.waiting}` : L.status[a.status] || "";
  $("card-body").innerHTML = `
    <h2>${esc(name)}</h2>
    <p class="near">${near.length ? `${esc(L.near)}: ${near.map(esc).join(", ")}` : ""}</p>
    <div class="callchip"><span class="dot" style="background:${LEVEL_COLORS[s.level]}"></span>${esc(L.levels[s.level])}</div>
    <p class="label">${esc(L.message)}</p>
    <div class="bubble">${esc(L.msg(params))}${meta ? `<span class="meta">${esc(meta)}</span>` : ""}</div>
    <p class="label">${esc(L.stripSchool)}</p>
    <svg class="strip" id="school-strip" role="img" aria-label="${esc(L.stripSchool)}"></svg>
    ${s.latest != null ? `<p class="latest">${L.latest(Math.round(s.latest))}</p>` : ""}
    ${a ? `<p class="demo">${esc(L.demo)}</p>` : ""}`;
  drawStrip($("school-strip"), s.hourly, s.clear);
  $("card").classList.add("open");
}

function select(id, fly) {
  state.selected = id;
  const s = current() && current().schools.find((x) => x.id === id);
  if (s && fly && state.map) state.map.flyTo({ center: [s.lon, s.lat], zoom: Math.max(state.map.getZoom(), 12.5) });
  renderCard();
}

function geojson() {
  const d = current();
  return {
    type: "FeatureCollection",
    features: d ? d.schools.map((s) => ({ type: "Feature", geometry: { type: "Point", coordinates: [s.lon, s.lat] },
      properties: { id: s.id, name: s.name, lvl: s.level } })) : [],
  };
}
function stationsGeojson() {
  const d = current();
  return { type: "FeatureCollection", features: d ? (d.stations || []).map(([id, name, lat, lon, latest]) => ({
    type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: { name, latest } })) : [] };
}

function addLayers() {
  const map = state.map;
  if (!map.isStyleLoaded() || map.getSource("schools")) return;
  map.addSource("stations", { type: "geojson", data: stationsGeojson() });
  map.addSource("schools", { type: "geojson", data: geojson() });
  fitOnce();
  const byLevel = (colors) => ["match", ["get", "lvl"], 0, colors[0], 1, colors[1], 2, colors[2], 3, colors[3], colors[4]];
  map.addLayer({ id: "schools", type: "circle", source: "schools", paint: {
    "circle-color": byLevel(LEVEL_COLORS), "circle-stroke-color": byLevel(LEVEL_RINGS), "circle-stroke-width": 1,
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 2.6, 11, 4, 13, 6.5, 15, 9],
    "circle-opacity": 0.92 } });
  map.addLayer({ id: "stations", type: "circle", source: "stations", paint: {
    "circle-color": "#ffffff", "circle-stroke-color": "#1e2b2f", "circle-stroke-width": 2,
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 3, 13, 5] } });
  drawPilotPins();
}
let fitted = false;
function fitOnce() {
  const d = current();
  if (fitted || !d || !d.schools.length) return;
  const lons = d.schools.map((s) => s.lon), lats = d.schools.map((s) => s.lat);
  state.map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], { padding: 24, duration: 0 });
  fitted = true;
}
function bindMapEvents() {
  const map = state.map;
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
  map.on("mousemove", "schools", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = e.features[0].properties;
    popup.setLngLat(e.lngLat).setHTML(`<b>${esc(p.name)}</b><br>${esc(t().levels[p.lvl])}`).addTo(map);
  });
  map.on("mouseleave", "schools", () => { map.getCanvas().style.cursor = ""; popup.remove(); });
  map.on("mousemove", "stations", (e) => {
    const p = e.features[0].properties;
    const v = Number(p.latest);
    popup.setLngLat(e.lngLat).setHTML(`<b>${esc(p.name)}</b>${p.latest != null && Number.isFinite(v) ? `<br>${Math.round(v)} µg/m³` : ""}`).addTo(map);
  });
  map.on("mouseleave", "stations", () => popup.remove());
  map.on("click", "schools", (e) => select(e.features[0].properties.id, false));
}
function refreshMap() {
  if (!state.map || !state.map.getSource("schools")) return;
  state.map.getSource("schools").setData(geojson());
  fitOnce();
  state.map.getSource("stations").setData(stationsGeojson());
  drawPilotPins();
}

function initMap(style) {
  if (typeof maplibregl === "undefined") return;
  let fellBack = false;
  const map = new maplibregl.Map({ container: "map", style: style || BLANK_STYLE, center: [77.16, 28.63], zoom: 10.1,
    attributionControl: { compact: true }, cooperativeGestures: false });
  state.map = map;
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  bindMapEvents();
  map.on("load", addLayers);
  map.on("styledata", addLayers);
  map.on("error", (e) => {
    if (fellBack || map.isStyleLoaded()) return;
    fellBack = true;
    map.setStyle(style === FALLBACK_STYLE ? BLANK_STYLE : FALLBACK_STYLE);
  });
}

async function getJSON(path) {
  try { const r = await fetch(path, { cache: "no-cache" }); return r.ok ? await r.json() : null; } catch { return null; }
}
function prepare(raw) {
  if (!raw) return null;
  return { ...raw, schools: raw.schools.map(unpack) };
}
function setMode(mode) {
  if (!state.data[mode]) return;
  state.mode = mode;
  $("btn-live").setAttribute("aria-pressed", String(mode === "live"));
  $("btn-replay").setAttribute("aria-pressed", String(mode === "replay"));
  const names = current().schools.map((s) => `<option value="${esc(s.name)}"></option>`);
  $("school-names").innerHTML = names.join("");
  renderBrief(); refreshMap(); renderCard();
}
function setLang(lang) {
  state.lang = lang;
  $("btn-en").setAttribute("aria-pressed", String(lang === "en"));
  $("btn-hi").setAttribute("aria-pressed", String(lang === "hi"));
  renderBrief(); renderCard();
}

async function pollAlerts() {
  const a = await getJSON("data/alerts.json");
  if (a && JSON.stringify(a) !== JSON.stringify(state.alerts)) {
    state.alerts = a; renderPilots(); refreshMap(); renderCard();
  }
}

async function main() {
  const [config, live, replay, alerts] = await Promise.all(["config", "live", "replay", "alerts"].map((n) => getJSON(`data/${n}.json`)));
  state.data = { live: prepare(live), replay: prepare(replay) };
  state.alerts = alerts || { days: {} };
  $("btn-live").disabled = !state.data.live;
  $("btn-replay").disabled = !state.data.replay;
  const params = new URLSearchParams(location.search);
  if (params.get("lang") === "hi") state.lang = "hi";
  state.mode = params.get("mode") === "replay" && state.data.replay ? "replay" : state.data.live ? "live" : "replay";
  initMap((config && config.mapStyle) || FALLBACK_STYLE);
  setLang(state.lang);
  setMode(state.mode);
  if (!current()) renderBrief();
  $("btn-live").addEventListener("click", () => setMode("live"));
  $("btn-replay").addEventListener("click", () => setMode("replay"));
  $("btn-en").addEventListener("click", () => setLang("en"));
  $("btn-hi").addEventListener("click", () => setLang("hi"));
  $("card-close").addEventListener("click", () => { state.selected = null; renderCard(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { state.selected = null; renderCard(); } });
  $("search").addEventListener("change", (e) => {
    const s = current() && current().schools.find((x) => x.name === e.target.value);
    if (s) select(s.id, true);
  });
  setInterval(pollAlerts, 20000);
}
main();
</script>
</body>
</html>
```

### `scripts/publish_site.sh` (Verbatim)

```bash
#!/usr/bin/env bash
# Upload the dashboard page and its map config. Safe to re-run; deploy.sh calls it after every deploy,
# and it's all you need after editing site/index.html.
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE=clearhour
REGION=ap-south-1
STACK=clearhour

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" --profile "$PROFILE" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET=$(output DataBucketName)
SITE_URL=$(output SiteUrl)
SITE_HOST=${SITE_URL#https://}
SITE_HOST=${SITE_HOST%/}

aws s3 cp site/index.html "s3://$BUCKET/site/index.html" --profile "$PROFILE" --only-show-errors \
  --content-type "text/html; charset=utf-8" --cache-control "max-age=300"

# Amazon Location map key: map tiles only, and only for this page's address. The key ships inside the page,
# so those restrictions are what protect it. It is never printed.
restrictions=$(printf '{"AllowActions":["geo-maps:*"],"AllowResources":["arn:aws:geo-maps:%s::provider/default"],"AllowReferers":["https://%s/*"]}' "$REGION" "$SITE_HOST")
config='{}'
if aws location describe-key --key-name clearhour-map --profile "$PROFILE" >/dev/null 2>&1 \
  || aws location create-key --key-name clearhour-map --no-expiry --restrictions "$restrictions" \
       --profile "$PROFILE" >/dev/null; then
  key=$(aws location describe-key --key-name clearhour-map --profile "$PROFILE" --query Key --output text)
  config=$(printf '{"mapStyle":"https://maps.geo.%s.amazonaws.com/v2/styles/Monochrome/descriptor?key=%s&color-scheme=Light"}' "$REGION" "$key")
else
  echo "Map key not created, so the page falls back to OpenFreeMap tiles." >&2
fi
printf '%s' "$config" | aws s3 cp - "s3://$BUCKET/site/data/config.json" --profile "$PROFILE" --only-show-errors \
  --content-type "application/json" --cache-control "max-age=300"

echo "Dashboard: $SITE_URL"
```

---

## Report

```
PHASE 3A REPORT
P3-T1: <n> tests; cfn-lint <ok>; build <ok>
P3-T2: deploy <ok>; SiteUrl <url>; map tiles <Amazon Location / fallback>; replay <ok>; live <ok>
Demo card params match the Send log: <yes/no>; Hindi <ok>; phone layout <ok>; console errors <none / list>
Screenshots: <paths>
Commits: <git log --oneline -3>
```
