# Phase 2 addendum: decision rule update, named pilots, deploy gate

Read `CLAUDE.md` first. Do A → D in order: check, commit, move on. Stop after D and report with the block at
the end. The P2-T6 deploy is approved once D's gate passes. P2-T7 still waits for me.

## Why the rule changes

The P2-T3 decision scores (winter 2025–26, 1,956 station-days) showed three things:

- **Assembly call.** The latest reading called "hold assembly indoors" better than the 08:00 forecast:
  accuracy 0.887 vs 0.862, with fewer misses (107 vs 126) and fewer needless calls (153 vs 191).
- **"Fine."** Of the 129 days the model called fine, only 48 were. Telling a school the air is fine when it
  isn't is the error we can least afford, so "fine" now also needs the latest reading at or below 90.
- **Very poor Clear Hours.** In winter the chosen hour averaged 151 µg/m³, which is CPCB "very poor". Naming
  it for outdoor activity without a caveat reads as "1 PM is safe", so the advice adds "Keep outdoor time
  short." whenever even the Clear Hour is forecast above 120.

The Clear Hour itself still comes from the model. Its pick cut pooled exposure by 25.1%, ahead of
`always_13` (22.9%) and CAMS (22.8%).

The patch and the code in B passed `ruff check`, `ruff format --check` and the tests here (pandas 3.0.6,
LightGBM 4.7.0, moto), and B's functions match `evaluate()` on synthetic winter and October folds.

---

## A: Decision rule → `src/clearhour/decide.py`, the Forecast and Decide handlers, tests

Save the patch below to `/tmp/rule.patch`, then from the repo root run `git apply --check /tmp/rule.patch`
and `git apply /tmp/rule.patch`. If a hunk fails because a file has drifted from the Verbatim version, make
the same change by hand.

What it changes:
- `decide(hourly, latest=None)`: `latest` is the newest measured PM2.5 near the school.
  - It makes the assembly call and guards "fine".
  - When assembly is held indoors, 08:00 can't also be the Clear Hour.
  - Every decision now carries `limit_outdoor`.
- `message_params`: when `limit_outdoor` is true, the advice gains "Keep outdoor time short." or
  "बच्चों को बाहर कम समय ही रखें।".
- Forecast handler: `forecast.json` gains `"latest"`, each station's `v_last`.
- Decide handler: `school_latest()` blends `latest` with the school's station weights, passes it to
  `decide`, and writes it to `decisions.json` for the dashboard.
- Tests: two new rule tests; the pipeline test checks `latest` end to end.

Add to DECISIONS.md:
- The assembly call and the "fine" check use the latest reading (the numbers above); the Clear Hour stays
  with the model.
- With assembly held indoors, 08:00 is never the Clear Hour.
- The advice adds "Keep outdoor time short." when even the Clear Hour is forecast above 120 µg/m³.

**Check:** `uv run pytest -q` (17 tests) and ruff clean.
Commit: `core: assembly call from the latest reading; keep-short advice`

### `/tmp/rule.patch` (Verbatim)

```diff
--- a/src/clearhour/decide.py
+++ b/src/clearhour/decide.py
@@ -8,7 +8,8 @@
 
 FINE_MAX = 90  # every school hour at or below: air is fine
 SEVERE_MIN = 250  # every school hour above: no safe window
-ASSEMBLY_INDOOR_MIN = 120  # assembly hour above: hold assembly indoors
+ASSEMBLY_INDOOR_MIN = 120  # assembly-time air above: hold assembly indoors
+VERY_POOR_MIN = 120  # even the Clear Hour above (CPCB "very poor"): keep outdoor time short
 
 EN_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
 EN_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
@@ -21,6 +22,7 @@
         "no_window": "No safe window today. Keep assembly, PE and recess indoors.",
         "assembly_in": "Hold assembly indoors.",
         "assembly_ok": "Assembly can stay outdoors.",
+        "keep_short": "Keep outdoor time short.",
         "any_time": "any time",
         "none_today": "none today",
         "replay": " (replay)",
@@ -30,6 +32,7 @@
         "no_window": "आज कोई सुरक्षित समय नहीं है। प्रार्थना सभा, पीटी और खेल अंदर ही कराएँ।",
         "assembly_in": "प्रार्थना सभा अंदर करें।",
         "assembly_ok": "प्रार्थना सभा बाहर हो सकती है।",
+        "keep_short": "बच्चों को बाहर कम समय ही रखें।",
         "any_time": "किसी भी समय",
         "none_today": "आज कोई नहीं",
         "replay": " (रीप्ले)",
@@ -37,15 +40,29 @@
 }
 
 
-def decide(hourly: dict[int, float]) -> dict:
-    """hourly: predicted PM2.5 per school hour (08..13). Returns the kind of day and the Clear Hour."""
+def decide(hourly: dict[int, float], latest: float | None = None) -> dict:
+    """hourly: predicted PM2.5 per school hour (08..13). latest: the newest measured PM2.5 near the school (the
+    04:00 IST bin, blended like the forecasts), or None. Returns the kind of day and the Clear Hour.
+
+    The Clear Hour comes from the forecast. The assembly call and the "fine" check use the latest reading when
+    there is one, because it called "hold assembly indoors" better than the 08:00 forecast in the backtest
+    (DECISIONS.md). When assembly is held indoors, 08:00 can't also be the Clear Hour.
+    """
     vals = {h: float(hourly[h]) for h in TARGET_HOURS}
-    if all(v <= FINE_MAX for v in vals.values()):
-        return {"kind": "fine", "clear_hour": None, "assembly_indoors": False}
+    now = vals[ASSEMBLY_HOUR] if latest is None else float(latest)
+    if all(v <= FINE_MAX for v in vals.values()) and now <= FINE_MAX:
+        return {"kind": "fine", "clear_hour": None, "assembly_indoors": False, "limit_outdoor": False}
     if all(v > SEVERE_MIN for v in vals.values()):
-        return {"kind": "no_window", "clear_hour": None, "assembly_indoors": True}
-    clear = min(vals, key=vals.get)
-    return {"kind": "clear_hour", "clear_hour": clear, "assembly_indoors": vals[ASSEMBLY_HOUR] > ASSEMBLY_INDOOR_MIN}
+        return {"kind": "no_window", "clear_hour": None, "assembly_indoors": True, "limit_outdoor": True}
+    indoors = now > ASSEMBLY_INDOOR_MIN
+    options = {h: v for h, v in vals.items() if not (indoors and h == ASSEMBLY_HOUR)}
+    clear = min(options, key=options.get)
+    return {
+        "kind": "clear_hour",
+        "clear_hour": clear,
+        "assembly_indoors": indoors,
+        "limit_outdoor": vals[clear] > VERY_POOR_MIN,
+    }
 
 
 def _clock(h: int) -> tuple[int, str]:
@@ -77,5 +94,7 @@
         advice, slot = t["no_window"], t["none_today"]
     else:
         advice = t["assembly_in"] if decision["assembly_indoors"] else t["assembly_ok"]
+        if decision.get("limit_outdoor"):
+            advice = f"{advice} {t['keep_short']}"
         slot = slot_label(decision["clear_hour"], lang)
     return [school_name, when, advice, slot]
--- a/src/clearhour/handlers/decide.py
+++ b/src/clearhour/handlers/decide.py
@@ -41,6 +41,13 @@
     return out
 
 
+def school_latest(school: dict, latest: dict[str, float]) -> float | None:
+    """The newest measured PM2.5 near the school, blended with the same weights; None if no station has one."""
+    pairs = [(w, latest[str(sid)]) for sid, w in school["near"] if str(sid) in latest]
+    total = sum(w for w, _ in pairs)
+    return sum(w * v for w, v in pairs) / total if total else None
+
+
 def handler(event, context):
     bucket = os.environ["DATA_BUCKET"]
     fc = json.loads(_s3.get_object(Bucket=bucket, Key=event["forecast_key"])["Body"].read())
@@ -50,11 +57,13 @@
     for sch in schools():
         hourly = school_hourly(sch, fc["stations"])
         if hourly:
+            now = school_latest(sch, fc.get("latest", {}))
             decisions[sch["id"]] = {
-                **rules.decide(hourly),
+                **rules.decide(hourly, latest=now),
                 "name": sch["name"],
                 "lat": sch["lat"],
                 "lon": sch["lon"],
+                "latest": None if now is None else round(now, 1),
                 "hourly": {str(h): round(v, 1) for h, v in hourly.items()},
             }
     decisions_key = event["forecast_key"].replace("forecast.json", "decisions.json")
--- a/src/clearhour/handlers/forecast.py
+++ b/src/clearhour/handlers/forecast.py
@@ -77,14 +77,17 @@
     rows["location_id"] = pd.Categorical(rows["location_id"], categories=meta["stations"])
     rows["pred"] = np.expm1(booster.predict(rows[features.FEATURES]))
     preds: dict[str, dict[str, float]] = {}
+    latest: dict[str, float] = {}  # each station's newest reading at or before 04:00 IST (v_last)
     for r in rows.itertuples():
         preds.setdefault(str(r.location_id), {})[str(r.target_hour)] = round(float(r.pred), 1)
+        latest[str(r.location_id)] = round(float(r.v_last), 1)
     body = {
         "day": str(day.date()),
         "source": source,
         "generated_at": pd.Timestamp.now(tz=IST).isoformat(timespec="seconds"),
         "median_lead_h": float(rows["lead_h"].median()),
         "stations": preds,
+        "latest": latest,
     }
     key = f"runs/{day.date()}/{source}/forecast.json"
     _s3.put_object(
--- a/tests/test_pipeline.py
+++ b/tests/test_pipeline.py
@@ -85,6 +85,8 @@
     forecast = _seed_model_and_obs(monkeypatch, day)
     out = forecast.handler({"source": "live", "as_of": "2025-11-13T05:30:00+05:30"}, None)
     assert out["stations"] == 3 and out["forecast_key"] == "runs/2025-11-13/live/forecast.json"
+    fc = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=out["forecast_key"])["Body"].read())
+    assert set(fc["latest"]) == set(fc["stations"])  # every forecast station carries its latest reading
 
     store.table().put_item(
         Item={"pk": "SCHOOL#node/1", "sk": "PROFILE", "name": "सर्वोदय विद्यालय", "phone": "919999999999", "lang": "hi"}
@@ -93,6 +95,8 @@
     monkeypatch.setattr(decide, "schools", lambda: SCHOOLS)
     res = decide.handler(out, None)
     assert res["schools"] == 1  # the far school has no station forecast
+    decisions = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=res["decisions_key"])["Body"].read())
+    assert decisions["node/1"]["latest"] is not None
     assert res["alerts"] == [{"school_id": "node/1", "day": "2025-11-13"}]
     alert = store.get_alert("node/1", "2025-11-13")
     assert alert["status"] == "pending" and alert["params"][1] == "गुरु 13 नवंबर"
--- a/tests/test_rules_and_messages.py
+++ b/tests/test_rules_and_messages.py
@@ -14,10 +14,29 @@
     assert decide(_hours(60, 70, 80, 90, 85, 75))["kind"] == "fine"
     assert decide(_hours(400, 380, 350, 320, 300, 260))["kind"] == "no_window"
     d = decide(_hours(300, 260, 220, 180, 150, 160))
-    assert d == {"kind": "clear_hour", "clear_hour": 12, "assembly_indoors": True}
+    assert d == {"kind": "clear_hour", "clear_hour": 12, "assembly_indoors": True, "limit_outdoor": True}
     assert decide(_hours(110, 100, 95, 92, 91, 140))["assembly_indoors"] is False
 
 
+def test_latest_reading_makes_the_assembly_call_and_guards_fine():
+    calm = _hours(100, 95, 90, 85, 80, 85)
+    assert decide(calm)["assembly_indoors"] is False
+    assert decide(calm, latest=150)["assembly_indoors"] is True  # the 04:00 reading overrules the 08:00 forecast
+    assert decide(_hours(150, 140, 130, 100, 90, 95), latest=100)["assembly_indoors"] is False
+    fine = _hours(60, 70, 80, 90, 85, 75)
+    assert decide(fine, latest=80)["kind"] == "fine"
+    held = decide(fine, latest=130)  # forecast says fine, but the air near the school is poor right now
+    assert held == {"kind": "clear_hour", "clear_hour": 9, "assembly_indoors": True, "limit_outdoor": False}
+
+
+def test_keep_short_advice_when_even_the_clear_hour_is_very_poor():
+    d = decide(_hours(300, 260, 220, 180, 150, 160))
+    en = message_params(d, "KV RK Puram", date(2025, 11, 13), "en")
+    assert en[2:] == ["Hold assembly indoors. Keep outdoor time short.", "12:00–1:00 PM"]
+    hi = message_params(d, "केवी आरके पुरम", date(2025, 11, 13), "hi")
+    assert hi[2] == "प्रार्थना सभा अंदर करें। बच्चों को बाहर कम समय ही रखें।"
+
+
 def test_labels_and_params_in_both_languages():
     assert slot_label(13, "en") == "1:00–2:00 PM"
     assert slot_label(11, "en") == "11:00 AM–12:00 PM"
```

---

## B: Score the production rule → `scripts/backtest.py`

Replace the decision-score functions you added from Snippet A with the block below (Verbatim). It adds:
- `rule_decisions`, which runs the production rule on each station-day
- a `clearhour` row in the strategy table: the hour the production rule names
- `kinds_forecast_only`: the kinds table without the latest reading, for comparison

Re-run the backtest. The P2-T3 numbers must not move, and the asserts against `evaluate()` must still pass.

Print and report, for winter and for October:
- the strategy table, with the new `clearhour` row
- `kinds` (the production rule) beside `kinds_forecast_only`

**Stop and show me** if the winter `clearhour` pooled cut is more than 0.01 below the `model` row's.
Commit: `analysis: score the production rule`

### Decision-score functions (Verbatim)

```python
import numpy as np
import pandas as pd

from clearhour.constants import ASSEMBLY_HOUR, TARGET_HOURS
from clearhour.decide import ASSEMBLY_INDOOR_MIN, decide

KINDS = ["fine", "clear_hour", "no_window"]
ASSEMBLY_COL = TARGET_HOURS.index(ASSEMBLY_HOUR)


def school_day_matrix(pred: pd.DataFrame) -> dict[str, np.ndarray]:
    """The station-days evaluate() scores (Mon-Fri, all six school hours observed), as arrays with one row per
    station-day and one column per school hour 08..13: the actual value, the model forecast and CAMS.
    "v_last" is one value per station-day: the latest reading the forecast started from."""
    cols = ["target", "pred", "cams_t", "v_last"]
    ok = pred[pred["target"].notna() & (pred["day"].dt.dayofweek < 5)]
    size = ok.groupby(["location_id", "day"], observed=True)["target"].transform("size")
    full = ok[size == len(TARGET_HOURS)]
    wide = full.set_index(["location_id", "day", "target_hour"])[cols].unstack("target_hour")
    m = {c: wide[c][TARGET_HOURS].to_numpy(dtype=float) for c in cols}
    m["v_last"] = m["v_last"][:, ASSEMBLY_COL]
    return m


def rule_decisions(m: dict[str, np.ndarray], use_latest: bool = True) -> list[dict]:
    """The production rule (clearhour.decide) on each station-day's forecast, with or without the latest reading."""
    return [
        decide(dict(zip(TARGET_HOURS, row, strict=True)), latest=float(now) if use_latest else None)
        for row, now in zip(m["pred"], m["v_last"], strict=True)
    ]


def strategy_table(pred: pd.DataFrame) -> pd.DataFrame:
    """Score each way of choosing the outdoor hour on the PM2.5 that actually occurred at the chosen hour.

    oracle is the cleanest school hour in hindsight; model and cams take the hour their forecast calls cleanest.
    clearhour is what the production rule names (on fine and no-window days, which name no hour, the model's pick).
    cut = 1 - PM2.5 at the chosen hour / PM2.5 at assembly (08:00), per station-day. pooled_cut = 1 - mean PM2.5
    at the chosen hour / mean PM2.5 at assembly, so the smoggiest days count the most.
    """
    m = school_day_matrix(pred)
    actual, cams = m["target"], m["cams_t"]
    n = len(actual)
    if n == 0:
        return pd.DataFrame()
    every_day = np.ones(n, dtype=bool)
    model_pick = np.argmin(m["pred"], axis=1)
    rule_pick = np.array(
        [
            TARGET_HOURS.index(d["clear_hour"]) if d["clear_hour"] is not None else p
            for d, p in zip(rule_decisions(m), model_pick, strict=True)
        ]
    )
    choices = {
        "oracle": (np.argmin(actual, axis=1), every_day),
        "model": (model_pick, every_day),
        "clearhour": (rule_pick, every_day),
        "cams": (np.argmin(np.where(np.isnan(cams), np.inf, cams), axis=1), ~np.isnan(cams).any(axis=1)),
        "always_12": (np.full(n, TARGET_HOURS.index(12)), every_day),
        "always_13": (np.full(n, TARGET_HOURS.index(13)), every_day),
    }
    top2 = np.argsort(actual, axis=1, kind="stable")[:, :2]
    rows = []
    for name, (pick, valid) in choices.items():
        i = np.flatnonzero(valid)
        if i.size == 0:
            continue
        got, assembly = actual[i, pick[i]], actual[i, ASSEMBLY_COL]
        pos = assembly > 0
        cut = 1 - got[pos] / assembly[pos]
        rows.append(
            {
                "strategy": name,
                "station_days": len(i),
                "hit_rate_top2": round(float((top2[i] == pick[i, None]).any(axis=1).mean()), 3),
                "median_cut": round(float(np.median(cut)), 3),
                "mean_cut": round(float(cut.mean()), 3),
                "pooled_cut": round(float(1 - got.mean() / assembly.mean()), 3),
                "pm25_at_pick": round(float(got.mean()), 1),
                "pm25_at_assembly": round(float(assembly.mean()), 1),
                "worse_than_assembly": round(float((got > assembly).mean()), 3),
            }
        )
    return pd.DataFrame(rows)


def assembly_flag(pred: pd.DataFrame) -> dict:
    """The 'hold assembly indoors' call (08:00 above ASSEMBLY_INDOOR_MIN) on school days: model vs persistence."""
    a = pred[
        (pred["target_hour"] == ASSEMBLY_HOUR)
        & (pred["day"].dt.dayofweek < 5)
        & pred["target"].notna()
        & pred["v_last"].notna()
    ]
    above = a["target"] > ASSEMBLY_INDOOR_MIN
    out = {"station_days": len(a), "actual_indoor_share": round(float(above.mean()), 3)}
    for name, col in (("model", "pred"), ("persistence", "v_last")):
        said = a[col] > ASSEMBLY_INDOOR_MIN
        out[name] = {
            "accuracy": round(float((said == above).mean()), 3),
            "missed_indoor": int((~said & above).sum()),  # said outdoors, but 08:00 was above the line
            "needless_indoor": int((said & ~above).sum()),
        }
    return out


def kind_table(pred: pd.DataFrame, use_latest: bool = True) -> dict:
    """Station-days by the kind of day the rule called (outer keys) and the kind the air turned out to be (inner)."""
    m = school_day_matrix(pred)
    called = [d["kind"] for d in rule_decisions(m, use_latest)]
    actual = [decide(dict(zip(TARGET_HOURS, row, strict=True)))["kind"] for row in m["target"]]
    table = pd.crosstab(pd.Series(called, name="called"), pd.Series(actual, name="actual"))
    table = table.reindex(index=KINDS, columns=KINDS, fill_value=0)
    return {c: {a: int(table.loc[c, a]) for a in KINDS} for c in KINDS}


def decision_scores(pred: pd.DataFrame) -> dict:
    return {
        "strategies": strategy_table(pred).to_dict(orient="records"),
        "assembly_flag": assembly_flag(pred),
        "kinds": kind_table(pred),
        "kinds_forecast_only": kind_table(pred, use_latest=False),
    }
```

---

## C: Pilots → `data/pilot_schools.csv`

Re-pick the five with the current rules, plus two more:

- **Named schools only.** Drop a school if every word in its name is a school-type word, ignoring digits and
  single letters. The school-type words are: government, govt, school, boys, girls, co, ed, coed, senior, sr,
  secondary, sec, middle, primary, high, higher, public, model, sarvodaya, sarvodya, kendriya, vidyalaya,
  kanya, bal, rajkiya, pratibha, vikas, nigam, mcd, ndmc, gbsss, gsss, ggsss, sss, skv, sbv, sector, block,
  no, number.
  - "Government school" and "Govt Boys Senior Secondary School" drop out.
  - "Kendriya Vidyalaya, RK Puram, Sector-2" stays, because of "RK Puram".
- **Delhi monitors only.** A pilot's nearest station must be inside Delhi, not in Noida, Gurugram, Ghaziabad
  or Faridabad.

Print the five with their stations and distances, and re-run P2-T5's pilot check. P2-T5's files don't
change.
Commit: `data: named pilot schools beside Delhi monitors`

---

## D: P2-T6, with the deploy gate

Do P2-T6 as briefed, with these changes:

1. **`scripts/deploy.sh`:** start it with `set -euo pipefail`. Pipe `sam deploy`'s output through this
   filter, so a key can't reach the terminal even if SAM echoes the parameter overrides:

   ```bash
   mask() { sed -e "s/${OPENAQ_API_KEY:-unset-openaq}/[openaq-key]/g" -e "s/${META_ACCESS_TOKEN:-unset-meta}/[meta-token]/g"; }
   sam deploy ... 2>&1 | mask
   ```

2. Write `samconfig.toml` by hand as in CLAUDE.md, swap in the template, then run cfn-lint and `sam build`.

3. **Gate.** Before `sam deploy`, all of these must hold:
   - Shreyash has told you the leaked key is rotated, and this prints exactly one line, `Active` with a
     CreateDate after 2026-10-07T22:00Z:
     `aws iam list-access-keys --user-name clearhour-cli --profile clearhour --query 'AccessKeyMetadata[].[Status,CreateDate]' --output text`
   - Tests, ruff, cfn-lint and `sam build` are all clean.
   - `WA_MODE` in `.env` is unset or `dry_run`.
   - `samconfig.toml` holds no parameter values, and `git status` shows no secrets staged.

   If the key isn't rotated yet, stop at the gate and tell him. Everything up to the build is done by then.

4. Deploy, upload the model and the archive, and run P2-T6's checks: the ingest invoke and the median live
   lag. If the 04:00 IST bin would usually be missing at 05:30, tell me, but don't change `LATEST_OBS_HOUR`
   yourself.

5. The DailyRun schedule fires at 05:30 IST. No school has a PROFILE yet, so it sends nothing. If it has
   already run by the time you report, include the execution's status and the forecast's `median_lead_h`.
   Don't wait for it.

Commit: `infra: daily pipeline (ingest, forecast, decide, send, inbound)`. Then stop. P2-T7 waits for me.

---

## Report

```
ADDENDUM REPORT
A: <n> tests pass; ruff <ok>
B winter: clearhour pooled <x> · median <x> · at pick <x> µg/m³ (model <x> · always_13 <x> · oracle <x>)
B winter kinds: production <table> | forecast-only <table>
B October: <the same two lines>
C pilots: <name · station · km> x5
D: build <ok>; gate <passed / waiting on key>; deploy <ok>; ingest written <n>, failed <n>; live lag median <h> h
D 05:30 run: <status and median_lead_h / not run yet>
Commits: <git log --oneline -6>
```
