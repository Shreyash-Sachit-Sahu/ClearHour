# Phase 2e brief: a sturdier morning run

Read `CLAUDE.md` first. Do M1 → M3 in order, then stop and report. The redeploy is approved, with `WA_MODE`
staying `dry_run`.

## What changes, and why

- **Open-Meteo outages.** A 503 failed the first live run. Now each Open-Meteo request is retried after 3, 10
  and 20 seconds, with a 20-second timeout per request. Connection errors, timeouts, 429 and 5xx are retried;
  any other error raises at once.
- **Forecast step.** It now also retries on any error, twice, one minute and then two minutes apart. Its
  Lambda gets 300 seconds instead of 180. Even the slowest path still has the alerts out well before 06:30.
- **WhatsApp fallback.** A new `WaSendAs` parameter (`template` by default) adds `text`, which sends the
  template's exact words as a plain message. WhatsApp allows a plain message only within 24 hours of the
  recipient's own last message. If the template is still in review when we record, Shreyash messages the
  business number first and we send as text. `render()` in `decide.py` holds the template bodies word for
  word, and a test checks the English one.
- **Hindi times.** Times on the page now read "शाम 7:00" in Hindi and "7:00 PM" in English, matching the
  alert's own style, where Hindi had shown "7:00 pm" before.

## M1: Code (Verbatim)

1. Save the patch below to `/tmp/morning.patch`. Run `git apply --check`, then `git apply`; make any failed
   hunk by hand.
2. Add `tests/test_meteo_retry.py` (Verbatim, below).
3. In `scripts/deploy.sh`, pass `WaSendAs=$WA_SEND_AS` only when `WA_SEND_AS` is set, like the other
   optional parameters. Add `WA_SEND_AS=` to `.env.example` with a one-line comment.
4. In CLAUDE.md, note that `WA_SEND_AS=text` is the template-review fallback and works only within 24 hours
   of the recipient's last message.

**Check:** `uv run pytest -q` (30 tests), ruff clean, cfn-lint clean, `sam build` ok.
Commit: `ops: retry weather outages, plain-text send fallback, Hindi times`

## M2: Deploy and check

1. Run `bash scripts/deploy.sh`. Calling it through `bash` works even if the executable bit goes missing
   again.
2. Start one live dry run. Report its status and the forecast's `model`, `blackout_h` and `obs_through`.
3. Retake the screenshots in `outputs/`, since the current ones still show the blue bar:
   - desktop Today
   - desktop replay with the first demo school's card open
   - the same replay view in Hindi
   - phone replay with the card open

Commit: `docs: fresh dashboard screenshots`

## M3: Stop and report

```
PHASE 2E REPORT
M1: <n> tests; cfn-lint ok; build ok
M2: deploy ok; live <status>; model <...>; blackout <h> h; obs_through <IST>
    screenshots: <paths>
Commits: <git log --oneline -3>
```

---

### `/tmp/morning.patch` (Verbatim)

```diff
--- a/site/index.html
+++ b/site/index.html
@@ -348,7 +348,14 @@
 function alertDay() { const d = current(); return d ? d.day + (state.mode === "replay" ? "-replay" : "") : null; }
 function alertsFor(id) { return (state.alerts.days[alertDay()] || []).find((a) => a.school_id === id); }
 function median(xs) { const v = xs.filter((x) => x != null).sort((a, b) => a - b); if (!v.length) return null; const m = v.length >> 1; return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2; }
-function timeIST(iso) { if (!iso) return ""; return new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "numeric", minute: "2-digit" }); }
+// Clock times in IST, written the way the alert writes them: "7:00 PM", or "शाम 7:00" in Hindi.
+function timeLabel(iso, lang) {
+  if (!iso) return "";
+  const d = new Date(new Date(iso).getTime() + 5.5 * 3600e3);
+  const h = d.getUTCHours(), m = String(d.getUTCMinutes()).padStart(2, "0"), h12 = (h % 12) || 12;
+  if (lang === "hi") return `${h < 4 ? "रात" : h < 12 ? "सुबह" : h < 16 ? "दोपहर" : h < 20 ? "शाम" : "रात"} ${h12}:${m}`;
+  return `${h12}:${m} ${h < 12 ? "AM" : "PM"}`;
+}
 
 // The school-day strip: six bars, the Clear Hour in blue, dashed lines at 120 and 250 µg/m³ labelled in a gutter.
 function drawStrip(svg, values, clearHour) {
@@ -394,11 +401,11 @@
     return;
   }
   $("replay-note").textContent = L.replayNote(longDay(d.day, state.lang));
-  let dateline = L.forecastFor(longDay(d.day, state.lang), timeIST(d.generated_at));
+  let dateline = L.forecastFor(longDay(d.day, state.lang), timeLabel(d.generated_at, state.lang));
   if (d.obs_through && new Date(d.obs_through) < new Date(`${d.day}T04:00:00+05:30`)) {
     const through = new Date(d.obs_through);
     const iso = new Date(through.getTime() + 5.5 * 3600e3).toISOString().slice(0, 10);
-    dateline += L.readingsUpTo(`${dayLabel(iso, state.lang)}, ${timeIST(d.obs_through)}`);
+    dateline += L.readingsUpTo(`${dayLabel(iso, state.lang)}, ${timeLabel(d.obs_through, state.lang)}`);
   }
   $("dateline").textContent = dateline;
   const staleNote = $("stale-note");
@@ -476,7 +483,7 @@
   const stationNames = new Map((d.stations || []).map(([sid, sname]) => [sid, sname]));
   const near = s.near.map((sid) => stationNames.get(sid)).filter(Boolean);
   let meta = "";
-  if (a) meta = a.acted ? L.actedAt(timeIST(a.acted_at)) : a.sent_at ? `${L.sentAt(timeIST(a.sent_at))} · ${L.waiting}` : L.status[a.status] || "";
+  if (a) meta = a.acted ? L.actedAt(timeLabel(a.acted_at, state.lang)) : a.sent_at ? `${L.sentAt(timeLabel(a.sent_at, state.lang))} · ${L.waiting}` : L.status[a.status] || "";
   $("card-body").innerHTML = `
     <h2>${esc(name)}</h2>
     <p class="near">${near.length ? `${esc(L.near)}: ${near.map(esc).join(", ")}` : ""}</p>
--- a/src/clearhour/decide.py
+++ b/src/clearhour/decide.py
@@ -43,6 +43,18 @@
 }
 
 
+# The approved template bodies, word for word (Phase 1 brief, A4). render() fills them for a plain-text send.
+TEMPLATE_BODY = {
+    "en": "ClearHour air update for {0} on {1}: {2} Cleanest hour for outdoor activity: {3}. "
+    "Reply 1 once you have moved outdoor activities.",
+    "hi": "ClearHour वायु सूचना – {0}, {1}: {2} बाहरी गतिविधियों के लिए सबसे साफ़ समय: {3}। गतिविधियाँ बदलने के बाद 1 लिखकर भेजें।",
+}
+
+
+def render(params: list[str], lang: str = "en") -> str:
+    return TEMPLATE_BODY.get(lang, TEMPLATE_BODY["en"]).format(*params)
+
+
 def decide(hourly: dict[int, float], latest: float | None = None, fixed_hour: int | None = None) -> dict:
     """hourly: predicted PM2.5 per school hour (08..13). latest: the newest measured PM2.5 near the school (the
     04:00 IST bin, blended like the forecasts), or None. Returns the kind of day and the Clear Hour.
--- a/src/clearhour/handlers/send.py
+++ b/src/clearhour/handlers/send.py
@@ -9,6 +9,7 @@
 import os
 from datetime import UTC, datetime
 
+from clearhour import decide as rules
 from clearhour import site, store, whatsapp
 
 
@@ -21,12 +22,16 @@
         return {"school_id": school_id, "status": "skipped", "reason": "claimed by another run"}
     profile = store.table().get_item(Key={"pk": f"SCHOOL#{school_id}", "sk": "PROFILE"})["Item"]
     lang = profile.get("lang", "en")
-    payload = whatsapp.template_payload(
-        profile["phone"],
-        list(alert["params"]),
-        name=os.environ.get("WA_TEMPLATE_NAME", "clearhour_daily_alert"),
-        lang=os.environ.get(f"WA_TEMPLATE_LANG_{lang.upper()}", lang),
-    )
+    params = list(alert["params"])
+    if os.environ.get("WA_SEND_AS") == "text":  # same words, plain message: only inside WhatsApp's 24-hour window
+        payload = whatsapp.text_payload(profile["phone"], rules.render(params, lang))
+    else:
+        payload = whatsapp.template_payload(
+            profile["phone"],
+            params,
+            name=os.environ.get("WA_TEMPLATE_NAME", "clearhour_daily_alert"),
+            lang=os.environ.get(f"WA_TEMPLATE_LANG_{lang.upper()}", lang),
+        )
     try:
         message_id = whatsapp.send(payload)
     except Exception as e:
--- a/src/clearhour/meteo.py
+++ b/src/clearhour/meteo.py
@@ -6,6 +6,8 @@
 
 from __future__ import annotations
 
+import time
+
 import pandas as pd
 import requests
 
@@ -14,11 +16,34 @@
 WEATHER_LIVE = "https://api.open-meteo.com/v1/forecast"
 AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"
 WEATHER_VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "boundary_layer_height"]
+RETRY_WAITS_S = (3, 10, 20)  # Open-Meteo has brief 503s; one must not cost the morning's alerts
 
 
-def _hourly(url: str, params: dict) -> pd.DataFrame:
-    r = requests.get(url, params=params, timeout=60)
+def _transient(e: requests.RequestException) -> bool:
+    if isinstance(e, requests.ConnectionError | requests.Timeout):
+        return True
+    status = e.response.status_code if e.response is not None else 0
+    return status == 429 or status >= 500
+
+
+def _get(url: str, params: dict) -> requests.Response:
+    """GET that retries connection errors, timeouts, 429 and 5xx a few times; anything else raises at once."""
+    for wait in RETRY_WAITS_S:
+        try:
+            r = requests.get(url, params=params, timeout=20)
+            r.raise_for_status()
+            return r
+        except requests.RequestException as e:
+            if not _transient(e):
+                raise
+            time.sleep(wait)
+    r = requests.get(url, params=params, timeout=20)
     r.raise_for_status()
+    return r
+
+
+def _hourly(url: str, params: dict) -> pd.DataFrame:
+    r = _get(url, params)
     h = r.json()["hourly"]
     df = pd.DataFrame(h)
     df["time"] = pd.to_datetime(df["time"], utc=True)
--- a/template.yaml
+++ b/template.yaml
@@ -17,6 +17,12 @@
   WaTemplateName:
     Type: String
     Default: clearhour_daily_alert
+  WaSendAs:
+    Type: String
+    Default: template
+    AllowedValues: [template, text]
+    Description: text sends the same words as a plain message; WhatsApp allows it only within 24 h of the
+      recipient's last message, so use it while the template awaits approval
   WaTemplateLangEn:
     Type: String
     Default: en
@@ -110,7 +116,7 @@
     Properties:
       PackageType: Image
       MemorySize: 2048
-      Timeout: 180
+      Timeout: 300
       Policies:
         - DynamoDBReadPolicy:
             TableName: !Ref ClearHourTable
@@ -157,6 +163,7 @@
       Environment:
         Variables:
           WA_MODE: !Ref WaMode
+          WA_SEND_AS: !Ref WaSendAs
           WA_PHONE_NUMBER_ID: !Ref WaPhoneNumberId
           WA_TEMPLATE_NAME: !Ref WaTemplateName
           WA_TEMPLATE_LANG_EN: !Ref WaTemplateLangEn
--- a/tests/test_pipeline.py
+++ b/tests/test_pipeline.py
@@ -109,6 +109,7 @@
     alert = store.get_alert("node/1", "2025-11-13")
     assert alert["status"] == "pending" and alert["params"][1] == "गुरु 13 नवंबर"
 
+    monkeypatch.setenv("WA_SEND_AS", "text")  # the 24-hour-window fallback sends the same words as plain text
     sent = send.handler(res["alerts"][0], None)
     assert sent == {"school_id": "node/1", "status": "sent", "message_id": "dry-run"}
     alerts = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key="site/data/alerts.json")["Body"].read())
--- a/tests/test_rules_and_messages.py
+++ b/tests/test_rules_and_messages.py
@@ -1,7 +1,7 @@
 import json
 from datetime import date
 
-from clearhour.decide import decide, message_params, slot_label
+from clearhour.decide import decide, message_params, render, slot_label
 from clearhour.handlers.ingest import hourly_means, lookback_hours
 from clearhour.whatsapp import parse_sns, send, template_payload
 
@@ -117,3 +117,15 @@
     assert lookback_hours({}) == 3 and lookback_hours(None) == 3
     assert lookback_hours({"lookback_hours": 48}) == 48
     assert lookback_hours({"lookback_hours": 500}) == 72
+
+
+def test_render_fills_the_template_body_word_for_word():
+    d = {"kind": "clear_hour", "clear_hour": 13, "assembly_indoors": True}
+    en = render(message_params(d, "KV RK Puram", date(2025, 11, 13), "en"), "en")
+    assert en == (
+        "ClearHour air update for KV RK Puram on Thu 13 Nov: Hold assembly indoors. "
+        "Cleanest hour for outdoor activity: 1:00–2:00 PM. Reply 1 once you have moved outdoor activities."
+    )
+    hi = render(message_params(d, "केवी", date(2025, 11, 13), "hi"), "hi")
+    assert hi.startswith("ClearHour वायु सूचना – केवी, गुरु 13 नवंबर: प्रार्थना सभा अंदर करें।")
+    assert hi.endswith("गतिविधियाँ बदलने के बाद 1 लिखकर भेजें।")
--- a/statemachine/daily.asl.json
+++ b/statemachine/daily.asl.json
@@ -73,6 +73,14 @@
           "IntervalSeconds": 5,
           "MaxAttempts": 3,
           "BackoffRate": 2
+        },
+        {
+          "ErrorEquals": [
+            "States.ALL"
+          ],
+          "IntervalSeconds": 60,
+          "MaxAttempts": 2,
+          "BackoffRate": 2
         }
       ],
       "Next": "Decide"
```

### `tests/test_meteo_retry.py` (Verbatim)

```python
"""Open-Meteo calls retry brief outages and give up at once on real errors."""

import pytest
import requests

from clearhour import meteo


class _Resp:
    def __init__(self, status: int):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return {"hourly": {"time": ["2026-10-08T00:00"], "pm2_5": [50.0]}}


def _fake_get(statuses: list[int], calls: list[int]):
    def get(url, params=None, timeout=None):
        calls.append(1)
        return _Resp(statuses[min(len(calls), len(statuses)) - 1])

    return get


def test_a_503_is_retried(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(meteo.requests, "get", _fake_get([503, 503, 200], calls))
    monkeypatch.setattr(meteo.time, "sleep", lambda s: None)
    assert meteo._get("https://example.test", {}).status_code == 200 and len(calls) == 3


def test_a_400_fails_at_once(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(meteo.requests, "get", _fake_get([400], calls))
    monkeypatch.setattr(meteo.time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError):
        meteo._get("https://example.test", {})
    assert len(calls) == 1


def test_a_lasting_outage_raises_after_four_tries(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(meteo.requests, "get", _fake_get([503], calls))
    monkeypatch.setattr(meteo.time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError):
        meteo._get("https://example.test", {})
    assert len(calls) == 4
```
