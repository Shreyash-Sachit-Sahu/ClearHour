# Phase 2: deploy brief (after the gate)

Read `CLAUDE.md` first. This replaces "re-run the gate and deploy". Do 1 → 5 in order, then stop and report
with the block at the end. P2-T7 step 3 (the real WhatsApp message) still waits for me.

**Phone numbers stay out of this chat:**
- Never print `PROFILE` items or `.env` values.
- After fix 1a below, dry-run logs show only the last four digits. Read the logs only once that fix is
  deployed.

## 1. Two fixes before the first deploy (Verbatim)

**a. Dry-run logs printed the full phone number.** `whatsapp.send` now logs `***` plus the last four digits.

**b. The morning run used stale readings.** `ScheduleV2` reads its cron in UTC, so the hourly ingest fired at
:40 IST. The last ingest before the 05:30 run was at 04:40, so the run worked from readings 1–2 hours old.
Two changes:
- The daily run now ingests first. A Choice state skips that step for archive replays, and if the ingest
  fails, the run carries on with what's stored.
- The hourly schedule now runs in IST, at :10.

The ingest also takes `{"lookback_hours": 48}` (capped at 72), so a backfill can stock DynamoDB for today's
live test.

Apply in this order:
1. Save the patch below to `/tmp/fresh.patch`. Run `git apply --check /tmp/fresh.patch`, then `git apply`.
   If a hunk fails, make the same change by hand.
2. Replace `statemachine/daily.asl.json` with the file below.

**Check:** `uv run pytest -q` (19 tests), ruff clean, cfn-lint clean, `sam build` ok.
Commit: `infra: ingest before the morning forecast; mask numbers in dry-run logs`

### `/tmp/fresh.patch` (Verbatim)

```diff
--- a/src/clearhour/handlers/ingest.py
+++ b/src/clearhour/handlers/ingest.py
@@ -1,7 +1,8 @@
 """Ingest Lambda (hourly): latest PM2.5 for each monitor from the OpenAQ API into DynamoDB, one item per IST hour.
 
 Readings are binned by the IST clock hour their period starts in, so the API's explicit periods need no
-start/end convention. The 3-hour lookback rewrites recent hours as late readings arrive.
+start/end convention. The 3-hour lookback rewrites recent hours as late readings arrive. An event of
+{"lookback_hours": 48} backfills (capped at 72 h, which keeps every sensor inside one page of 1,000 readings).
 """
 
 from __future__ import annotations
@@ -47,9 +48,14 @@
     return {k: (sum(v) / len(v), len(v)) for k, v in buckets.items()}
 
 
+def lookback_hours(event) -> int:
+    asked = event.get("lookback_hours") if isinstance(event, dict) else None
+    return max(1, min(int(asked or os.environ.get("LOOKBACK_HOURS", "3")), 72))
+
+
 def handler(event, context):
     now = datetime.now(UTC)
-    since = now - timedelta(hours=int(os.environ.get("LOOKBACK_HOURS", "3")))
+    since = now - timedelta(hours=lookback_hours(event))
     written, failed = 0, []
     for s in stations():
         try:
--- a/src/clearhour/whatsapp.py
+++ b/src/clearhour/whatsapp.py
@@ -47,8 +47,9 @@
         )
         with urllib.request.urlopen(req, timeout=20) as r:
             return json.load(r)["messages"][0]["id"]
-    if mode == "dry_run":
-        print(json.dumps({"dry_run_payload": payload}, ensure_ascii=False))
+    if mode == "dry_run":  # the log keeps only the number's last four digits
+        shown = {**payload, "to": "***" + str(payload.get("to", ""))[-4:]}
+        print(json.dumps({"dry_run_payload": shown}, ensure_ascii=False))
         return "dry-run"
     raise ValueError(f"unknown WA_MODE {mode!r}")
 
--- a/template.yaml
+++ b/template.yaml
@@ -103,6 +103,7 @@
           Type: ScheduleV2
           Properties:
             ScheduleExpression: cron(10 * * * ? *)
+            ScheduleExpressionTimezone: Asia/Kolkata
 
   ForecastFunction:
     Type: AWS::Serverless::Function
@@ -203,11 +204,14 @@
     Properties:
       DefinitionUri: statemachine/daily.asl.json
       DefinitionSubstitutions:
+        IngestFunctionArn: !GetAtt IngestFunction.Arn
         ForecastFunctionArn: !GetAtt ForecastFunction.Arn
         DecideFunctionArn: !GetAtt DecideFunction.Arn
         SendFunctionArn: !GetAtt SendFunction.Arn
       Policies:
         - LambdaInvokePolicy:
+            FunctionName: !Ref IngestFunction
+        - LambdaInvokePolicy:
             FunctionName: !Ref ForecastFunction
         - LambdaInvokePolicy:
             FunctionName: !Ref DecideFunction
--- a/tests/test_rules_and_messages.py
+++ b/tests/test_rules_and_messages.py
@@ -2,8 +2,8 @@
 from datetime import date
 
 from clearhour.decide import decide, message_params, slot_label
-from clearhour.handlers.ingest import hourly_means
-from clearhour.whatsapp import parse_sns, template_payload
+from clearhour.handlers.ingest import hourly_means, lookback_hours
+from clearhour.whatsapp import parse_sns, send, template_payload
 
 
 def _hours(*vals):
@@ -95,3 +95,17 @@
     out = hourly_means(res)
     assert out["2025-11-13T02:30:00+00:00"] == (340 / 3, 3)
     assert out["2025-11-13T03:30:00+00:00"] == (50.0, 1)  # -999 (missing) dropped
+
+
+def test_dry_run_log_masks_the_phone_number(monkeypatch, capsys):
+    monkeypatch.setenv("WA_MODE", "dry_run")
+    assert send(template_payload("919999990123", ["a", "b", "c", "d"], name="t", lang="en")) == "dry-run"
+    out = capsys.readouterr().out
+    assert "919999990123" not in out and "***0123" in out
+
+
+def test_ingest_lookback_defaults_to_three_hours_and_caps_backfills(monkeypatch):
+    monkeypatch.delenv("LOOKBACK_HOURS", raising=False)
+    assert lookback_hours({}) == 3 and lookback_hours(None) == 3
+    assert lookback_hours({"lookback_hours": 48}) == 48
+    assert lookback_hours({"lookback_hours": 500}) == 72
```

### `statemachine/daily.asl.json` (Verbatim)

```json
{
  "Comment": "ClearHour daily run: fresh readings, forecast every station, decide every school, send the subscribed schools' alerts",
  "StartAt": "Fresh",
  "States": {
    "Fresh": {
      "Type": "Choice",
      "Comment": "Live runs pull the newest readings first; archive replays skip it",
      "Choices": [
        {
          "And": [
            {
              "Variable": "$.source",
              "IsPresent": true
            },
            {
              "Variable": "$.source",
              "StringEquals": "archive"
            }
          ],
          "Next": "Forecast"
        }
      ],
      "Default": "Ingest"
    },
    "Ingest": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "${IngestFunctionArn}",
        "Payload": {}
      },
      "ResultPath": null,
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 3,
          "BackoffRate": 2
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.ingest_error",
          "Next": "Forecast"
        }
      ],
      "Next": "Forecast"
    },
    "Forecast": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "${ForecastFunctionArn}",
        "Payload.$": "$"
      },
      "OutputPath": "$.Payload",
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 3,
          "BackoffRate": 2
        }
      ],
      "Next": "Decide"
    },
    "Decide": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "${DecideFunctionArn}",
        "Payload.$": "$"
      },
      "OutputPath": "$.Payload",
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException"
          ],
          "IntervalSeconds": 5,
          "MaxAttempts": 3,
          "BackoffRate": 2
        }
      ],
      "Next": "SendAlerts"
    },
    "SendAlerts": {
      "Type": "Map",
      "ItemsPath": "$.alerts",
      "MaxConcurrency": 5,
      "ItemProcessor": {
        "ProcessorConfig": {
          "Mode": "INLINE"
        },
        "StartAt": "Send",
        "States": {
          "Send": {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {
              "FunctionName": "${SendFunctionArn}",
              "Payload.$": "$"
            },
            "OutputPath": "$.Payload",
            "Retry": [
              {
                "ErrorEquals": [
                  "States.ALL"
                ],
                "IntervalSeconds": 10,
                "MaxAttempts": 2,
                "BackoffRate": 2
              }
            ],
            "Catch": [
              {
                "ErrorEquals": [
                  "States.ALL"
                ],
                "ResultPath": "$.error",
                "Next": "SendFailed"
              }
            ],
            "End": true
          },
          "SendFailed": {
            "Type": "Pass",
            "End": true
          }
        }
      },
      "ResultPath": "$.sent",
      "End": true
    }
  }
}
```

## 2. Gate, then deploy

Re-run the D gate from the addendum: key rotated, exactly one Active key created after 2026-10-07T22:00Z,
everything clean, `WA_MODE` unset or `dry_run`. Then run `./scripts/deploy.sh`, with the model and archive
uploads, and confirm `git diff samconfig.toml` holds no key.

## 3. Stock the store and measure the lag

1. Backfill: `sam remote invoke IngestFunction --stack-name clearhour --profile clearhour --event '{"lookback_hours": 48}'`.
   Report `written` and `failed`.
2. Lag. For each station, find:
   - (a) the newest hour with any reading
   - (b) the newest hour with at least three of its four 15-minute readings (`n >= 3`). Skip stations that
     report hourly.

   Report the median of (now − hour start) for (a) and for (b), and the time you measured.
   Don't change `LATEST_OBS_HOUR`. Tomorrow's 05:30 run records `median_lead_h`, and that's what I'll
   decide on.

## 4. P2-T7 steps 1 and 2: pilots and the two dry runs

- **Pilot display names.** Add a `display_name` column to `data/pilot_schools.csv`, and have
  `seed_pilots.py` write it as the PROFILE `name`. Keep `osm_id` and the OSM name as they are. Use:
  - Kendriya Vidyalaya, RK Puram Sector 2
  - Rani Chenamma Sarvodaya Kanya Vidyalaya
  - Kendriya Vidyalaya Shahdara
  - Sarvodaya Kanya Vidyalaya, Noor Nagar
  - Swami Dayanand Govt. Sarvodaya Vidyalaya
- **Seed** from `PILOT_PHONES` and `PILOT_LANGS` in `.env`. Shreyash adds his number there himself. If
  `PILOT_PHONES` is empty, stop and tell him; don't ask for the number in chat.
- **Dry runs**, as in P2-T7 step 2: `{"source": "archive", "as_of": "2025-11-13", "resend": true}`, then
  `{"source": "live", "resend": true}`. For each one, report:
  - the execution status
  - the run's `median_lead_h`
  - the four template parameters from the Send log
  - for the live run, whether the Ingest step ran without error

Commit: `ops: seed pilots and first dry runs`

## 5. Stop

The next scheduled run is 05:30 IST tomorrow. With no WhatsApp yet it stays a dry run, which is fine.

## Report

```
DEPLOY REPORT
1: <n> tests; cfn-lint <ok>; build <ok>
2: gate <ok>; deploy <ok>; stack outputs <DataBucketName, DailyRunArn>; samconfig diff <clean>
3: backfill written <n>, failed <n>; lag at <time IST>: any reading <h> h, mostly complete <h> h (median)
4: replay <status> lead <h> params <4 values> | live <status> lead <h> ingest <ok/error> params <4 values>
Commits: <git log --oneline -4>
```
