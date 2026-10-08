# Phase 2d brief: a fresh model and a stale model

Read `CLAUDE.md` first. This replaces S2–S4 of `phase-2c-stale-readings.md`; its S1 stays as committed. Do
T1 → T4 in order: check, commit, then move to the next. Stop after T4 and report with the block at the end.
The redeploy in T3 is approved, with `WA_MODE` staying `dry_run`.

## The decision

Your option 1: two models. Fresh mornings keep today's model by construction, so they stay at 49.1 MAE and
a 0.250 pooled cut.
- **Fresh model:** the current one, trained on blackout 0. It serves any morning whose blackout is under
  6 hours (`STALE_FROM_H`).
- **Stale model:** trained only on the 6–36 hour blackouts. It serves everything from 6 hours up.
- **The morning's blackout picks the model.** `forecast.json` records which one ran.

On stale mornings the forecast still sets the level of the day: fine, assembly indoors, keep outdoor time
short, or no safe window. The hour is a different story. Your S2 table shows the forecast's pick trailing
"always 1 PM" (about 0.229 pooled) at every stale blackout from 12 hours on.

So the Clear Hour on a stale morning defaults to 1 PM, the school hour that's cleanest most often. The page
says so, because there 1 PM may not be the lowest bar. The default is one constant, `STALE_CLEAR_HOUR`, and
T1's backtest of the stale-only model settles it with a rule fixed in advance (below).

Skip options 2 and 3: two models guarantee fresh mornings, and the deadline doesn't leave room for tuning.

---

## T1: Code, then the backtest

1. Save the patch below to `/tmp/two.patch`. Run `git apply --check`, then `git apply`; make any failed hunk by
   hand. What it changes:
   - `features.py`: adds `STALE_FROM_H` and `STALE_BLACKOUTS_H`.
   - Forecast:
     - loads the fresh or stale model by the morning's blackout
     - refuses a blackout beyond the stale model's `blackouts_h`
     - writes `"model"` into `forecast.json`
   - `decide.py`: adds `decide(..., fixed_hour=None)` and `STALE_CLEAR_HOUR = 13`.
   - Decide handler: passes `STALE_CLEAR_HOUR` as the fixed hour on stale mornings.
   - `site.py`: adds `model` and `stale_hour` to the day file.
   - Tests: a fixed-hour test, and both models in the pipeline test.

   **Check:** 26 tests, ruff clean. Commit: `core: fresh and stale models`
2. In `scripts/backtest.py`, keep your blackout-tagged rows and run two walk-forwards over the winter folds:
   - **Fresh:** train and test on blackout-0 rows only. Assert it reproduces P2-T3: model MAE 49.1 and
     `clearhour` pooled cut 0.250.
   - **Stale:** train on the `STALE_BLACKOUTS_H` rows and test on each of those blackouts.

   Do the same for the October fold. Put blackout 0 at the top level of `backtest.json` and the rest under
   `"by_blackout"`.
3. **The rule fixed in advance.** For each of the six stale blackouts, compare the stale model's `clearhour`
   pooled cut (its own pick) with `always_13`'s.
   - If `always_13` is at least as high at four or more of them, keep `STALE_CLEAR_HOUR = 13`.
   - Otherwise, set it to `None`, which keeps the forecast's pick.

   Log the outcome in DECISIONS.md with the six pairs of numbers. If it's `None`, re-run the tests.

Commit: `analysis: backtest fresh and stale models by blackout`

## T2: Train both models

`train_final.py` trains:
- **The fresh model, as now.** `features.json` gains `"blackouts_h": [0]`.
- **The stale model** on the `STALE_BLACKOUTS_H` rows, through the same last full day. It goes to
  `models/clearhour-lgbm-stale.txt` and `models/features-stale.json`, with
  `"blackouts_h": list(STALE_BLACKOUTS_H)`.

Commit `features-stale.json`; `models/*.txt` stays gitignored. Have `scripts/deploy.sh` upload both model files
and both JSON files to `s3://<bucket>/models/`.
Commit: `model: fresh and stale models`

## T3: Page, deploy, live dry run

1. Save the page patch below to `/tmp/page.patch` and apply it the same way. It replaces the page patch in the
   2c brief, so skip that one. It does three things:
   - marks the Clear Hour in ink
   - adds the readings-freshness line
   - adds the stale-morning note, in both languages
2. Run `./scripts/deploy.sh`.
3. Start a live dry run with `{"source": "live", "resend": true}`. Expect SUCCEEDED with `"model": "stale"`, then
   report:
   - `blackout_h`, `obs_through` and `median_lead_h`
   - the four params
4. Open the page and check that Today shows the freshness line and the stale note, in English and in Hindi.

Commit: `dashboard: Clear Hour in ink, freshness and stale-morning notes`

## T4: Stop and report

Tomorrow's 05:30 run is the first scheduled one on the new code.

```
PHASE 2D REPORT
T1: <n> tests; fresh reproduces P2-T3 <yes/no>
    stale by blackout (h): MAE model/persist/CAMS | clearhour pooled vs always_13 | hit | median cut
       6: ...   12: ...   18: ...   24: ...   30: ...   36: ...
    STALE_CLEAR_HOUR = <13 / None> (always_13 >= model at <k>/6)
T2: rows fresh <n>, stale <n>; both trained through <date>
T3: deploy <ok>; live <status>; model <fresh/stale>; blackout <h> h; obs_through <IST>; params <4 values>
    page: freshness line <ok>; stale note <ok>; Hindi <ok>
Commits: <git log --oneline -5>
```

---

### `/tmp/two.patch` (Verbatim)

```diff
--- a/src/clearhour/decide.py
+++ b/src/clearhour/decide.py
@@ -10,6 +10,9 @@
 SEVERE_MIN = 250  # every school hour above: no safe window
 ASSEMBLY_INDOOR_MIN = 120  # assembly-time air above: hold assembly indoors
 VERY_POOR_MIN = 120  # even the Clear Hour above (CPCB "very poor"): keep outdoor time short
+# On a stale morning (no readings from the night), name this hour instead of the forecast's cleanest one, or None to
+# keep the forecast's pick. Set from the backtest by blackout; see DECISIONS.md.
+STALE_CLEAR_HOUR: int | None = 13
 
 EN_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
 EN_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
@@ -40,13 +43,14 @@
 }
 
 
-def decide(hourly: dict[int, float], latest: float | None = None) -> dict:
+def decide(hourly: dict[int, float], latest: float | None = None, fixed_hour: int | None = None) -> dict:
     """hourly: predicted PM2.5 per school hour (08..13). latest: the newest measured PM2.5 near the school (the
     04:00 IST bin, blended like the forecasts), or None. Returns the kind of day and the Clear Hour.
 
     The Clear Hour comes from the forecast. The assembly call and the "fine" check use the latest reading when
     there is one, because it called "hold assembly indoors" better than the 08:00 forecast in the backtest
-    (DECISIONS.md). When assembly is held indoors, 08:00 can't also be the Clear Hour.
+    (DECISIONS.md). When assembly is held indoors, 08:00 can't also be the Clear Hour. fixed_hour, when given,
+    is named as the Clear Hour instead of the forecast's pick (stale mornings).
     """
     vals = {h: float(hourly[h]) for h in TARGET_HOURS}
     now = vals[ASSEMBLY_HOUR] if latest is None else float(latest)
@@ -56,7 +60,7 @@
         return {"kind": "no_window", "clear_hour": None, "assembly_indoors": True, "limit_outdoor": True}
     indoors = now > ASSEMBLY_INDOOR_MIN
     options = {h: v for h, v in vals.items() if not (indoors and h == ASSEMBLY_HOUR)}
-    clear = min(options, key=options.get)
+    clear = fixed_hour if fixed_hour in options else min(options, key=options.get)
     return {
         "kind": "clear_hour",
         "clear_hour": clear,
--- a/src/clearhour/features.py
+++ b/src/clearhour/features.py
@@ -16,6 +16,10 @@
 # trains on these blackouts (hours of missing readings before 04:00) and serves whichever one the data shows.
 BLACKOUTS_H = (0, 6, 12, 18, 24, 30, 36)
 MAX_BLACKOUT_H = BLACKOUTS_H[-1]
+# One model trained on every blackout lost too much on fresh mornings, so there are two: the fresh model (blackout 0)
+# and the stale model (these blackouts). The forecast picks one by the morning's blackout.
+STALE_FROM_H = 6
+STALE_BLACKOUTS_H = tuple(b for b in BLACKOUTS_H if b >= STALE_FROM_H)
 FRESH_LEAD_H = ASSEMBLY_HOUR - LATEST_OBS_HOUR + MAX_STALENESS_H  # 08:00 leads up to this: a reading from the night
 MET_COLUMNS = ["pm2_5", "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "boundary_layer_height"]
 
--- a/src/clearhour/handlers/decide.py
+++ b/src/clearhour/handlers/decide.py
@@ -55,12 +55,13 @@
 
     decisions = {}
     all_schools = schools()
+    fixed = rules.STALE_CLEAR_HOUR if fc.get("model") == "stale" else None
     for sch in all_schools:
         hourly = school_hourly(sch, fc["stations"])
         if hourly:
             now = school_latest(sch, fc.get("latest", {}))
             decisions[sch["id"]] = {
-                **rules.decide(hourly, latest=now),
+                **rules.decide(hourly, latest=now, fixed_hour=fixed),
                 "name": sch["name"],
                 "lat": sch["lat"],
                 "lon": sch["lon"],
--- a/src/clearhour/handlers/forecast.py
+++ b/src/clearhour/handlers/forecast.py
@@ -19,17 +19,22 @@
 from clearhour.constants import IST
 
 _s3 = boto3.client("s3")
-_MODEL: tuple[lgb.Booster, dict] | None = None
+MODEL_FILES = {
+    "fresh": ("models/clearhour-lgbm.txt", "models/features.json"),
+    "stale": ("models/clearhour-lgbm-stale.txt", "models/features-stale.json"),
+}
+_MODELS: dict[str, tuple[lgb.Booster, dict]] = {}
 
 
-def model() -> tuple[lgb.Booster, dict]:
-    global _MODEL
-    if _MODEL is None:
+def model(kind: str = "fresh") -> tuple[lgb.Booster, dict]:
+    """The fresh model serves mornings that have the night's readings; the stale model, mornings that don't."""
+    if kind not in _MODELS:
         bucket = os.environ["DATA_BUCKET"]
-        text = _s3.get_object(Bucket=bucket, Key="models/clearhour-lgbm.txt")["Body"].read().decode()
-        meta = json.loads(_s3.get_object(Bucket=bucket, Key="models/features.json")["Body"].read())
-        _MODEL = (lgb.Booster(model_str=text), meta)
-    return _MODEL
+        text_key, meta_key = MODEL_FILES[kind]
+        text = _s3.get_object(Bucket=bucket, Key=text_key)["Body"].read().decode()
+        meta = json.loads(_s3.get_object(Bucket=bucket, Key=meta_key)["Body"].read())
+        _MODELS[kind] = (lgb.Booster(model_str=text), meta)
+    return _MODELS[kind]
 
 
 def stations() -> list[dict]:
@@ -66,15 +71,16 @@
 def handler(event, context):
     day = run_day(event)
     source = event.get("source", "live")
-    booster, meta = model()
-    if meta["features"] != features.FEATURES:
-        raise RuntimeError("model was trained on a different feature list; retrain")
     hourly = live_obs(day) if source == "live" else archive_obs(day)
     met = meteo.fetch((day - pd.Timedelta(days=1)).date().isoformat(), day.date().isoformat(), live=source == "live")
     blackout = features.blackout_for(hourly, day)
-    trained_up_to = max(meta.get("blackouts_h", [0]))
-    if blackout > trained_up_to:
-        raise RuntimeError(f"the newest reading is {blackout} h before 04:00 IST; the model covers {trained_up_to} h")
+    kind = "fresh" if blackout < features.STALE_FROM_H else "stale"
+    booster, meta = model(kind)
+    if meta["features"] != features.FEATURES:
+        raise RuntimeError("model was trained on a different feature list; retrain")
+    covers = max(meta.get("blackouts_h", [0]))
+    if kind == "stale" and blackout > covers:
+        raise RuntimeError(f"the newest reading is {blackout} h before 04:00 IST; the stale model covers {covers} h")
     rows = features.build_rows(hourly, met, [day], blackout_h=blackout)
     if rows.empty:
         raise RuntimeError(f"no station had a reading within {features.MAX_STALENESS_H} h of the newest one")
@@ -93,6 +99,7 @@
         "median_lead_h": float(rows["lead_h"].median()),
         "blackout_h": blackout,
         "obs_through": (day + pd.Timedelta(hours=features.LATEST_OBS_HOUR - blackout)).isoformat(),
+        "model": kind,
         "stations": preds,
         "latest": latest,
     }
@@ -107,6 +114,7 @@
                 "source": source,
                 "stations": len(preds),
                 "blackout_h": blackout,
+                "model": kind,
                 "median_lead_h": body["median_lead_h"],
             }
         )
--- a/src/clearhour/site.py
+++ b/src/clearhour/site.py
@@ -17,6 +17,7 @@
 
 from clearhour import store
 from clearhour.constants import TARGET_HOURS
+from clearhour.decide import STALE_CLEAR_HOUR
 
 PREFIX = "site/data/"
 ALERTS_PER_SCHOOL = 14
@@ -71,6 +72,8 @@
         "generated_at": fc["generated_at"],
         "median_lead_h": fc.get("median_lead_h"),
         "obs_through": fc.get("obs_through"),
+        "model": fc.get("model", "fresh"),
+        "stale_hour": STALE_CLEAR_HOUR if fc.get("model") == "stale" else None,
         "hours": TARGET_HOURS,
         "stations": [
             [s["location_id"], s["name"], s["lat"], s["lon"], latest.get(str(s["location_id"]))] for s in stations()
--- a/tests/test_pipeline.py
+++ b/tests/test_pipeline.py
@@ -58,25 +58,26 @@
     hourly, met = make_synthetic(n_stations=3, start="2025-10-01", end="2025-11-20")
     stations = [int(s) for s in sorted(hourly["location_id"].unique())]  # plain ints: they go into JSON
     train_days = pd.date_range("2025-10-03", "2025-11-10", tz="Asia/Kolkata")
-    rows = pd.concat(
-        [with_station_category(features.build_rows(hourly, met, train_days, blackout_h=b), stations) for b in (0, 24)],
-        ignore_index=True,
-    )
-    assert not rows.empty
-    booster = fit(rows, num_rounds=30)
     s3 = boto3.client("s3")
-    s3.put_object(Bucket=BUCKET, Key="models/clearhour-lgbm.txt", Body=booster.model_to_string().encode())
-    s3.put_object(
-        Bucket=BUCKET,
-        Key="models/features.json",
-        Body=json.dumps({"features": features.FEATURES, "stations": stations, "blackouts_h": [0, 24]}).encode(),
-    )
+    for suffix, blackouts in (("", [0]), ("-stale", [12, 24])):  # the fresh and the stale model
+        rows = pd.concat(
+            [
+                with_station_category(features.build_rows(hourly, met, train_days, blackout_h=b), stations)
+                for b in blackouts
+            ],
+            ignore_index=True,
+        )
+        assert not rows.empty
+        booster = fit(rows, num_rounds=30)
+        meta = {"features": features.FEATURES, "stations": stations, "blackouts_h": blackouts}
+        s3.put_object(Bucket=BUCKET, Key=f"models/clearhour-lgbm{suffix}.txt", Body=booster.model_to_string().encode())
+        s3.put_object(Bucket=BUCKET, Key=f"models/features{suffix}.json", Body=json.dumps(meta).encode())
     until = obs_until if obs_until is not None else day + pd.Timedelta(hours=5)
     recent = hourly[(hourly["hour_ist"] >= day - pd.Timedelta(days=2)) & (hourly["hour_ist"] < until)]
     for r in recent.itertuples():
         store.put_obs(int(r.location_id), r.hour_ist.tz_convert("UTC").isoformat(), float(r.pm25), 4)
     monkeypatch.setattr(forecast, "_s3", s3)
-    monkeypatch.setattr(forecast, "_MODEL", None)
+    monkeypatch.setattr(forecast, "_MODELS", {})
     monkeypatch.setattr(forecast, "stations", lambda: STATIONS)
     monkeypatch.setattr(forecast.meteo, "fetch", lambda *a, **k: met)
     return forecast
@@ -91,6 +92,7 @@
     assert out["stations"] == 3 and out["forecast_key"] == "runs/2025-11-13/live/forecast.json"
     fc = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=out["forecast_key"])["Body"].read())
     assert set(fc["latest"]) == set(fc["stations"])  # every forecast station carries its latest reading
+    assert fc["model"] == "fresh" and fc["blackout_h"] == 0
 
     store.table().put_item(
         Item={"pk": "SCHOOL#node/1", "sk": "PROFILE", "name": "सर्वोदय विद्यालय", "phone": "919999999999", "lang": "hi"}
@@ -135,8 +137,12 @@
     forecast = _seed_model_and_obs(monkeypatch, day, obs_until=day - pd.Timedelta(hours=12))  # newest: 11:00 yesterday
     out = forecast.handler({"source": "live", "as_of": "2025-11-13T05:30:00+05:30"}, None)
     fc = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=out["forecast_key"])["Body"].read())
-    assert fc["blackout_h"] == 17 and fc["obs_through"].startswith("2025-11-12T11:00")
+    assert fc["blackout_h"] == 17 and fc["obs_through"].startswith("2025-11-12T11:00") and fc["model"] == "stale"
     assert fc["latest"] == {}  # nothing from the night, so no reading drives the assembly call
     monkeypatch.setattr(decide, "_s3", boto3.client("s3"))
     monkeypatch.setattr(decide, "schools", lambda: SCHOOLS)
-    assert decide.handler(out, None)["schools"] == 1
+    res = decide.handler(out, None)
+    assert res["schools"] == 1
+    decisions = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=res["decisions_key"])["Body"].read())
+    if decisions["node/1"]["kind"] == "clear_hour":
+        assert decisions["node/1"]["clear_hour"] == 13  # stale mornings name 1 PM
--- a/tests/test_rules_and_messages.py
+++ b/tests/test_rules_and_messages.py
@@ -29,6 +29,14 @@
     assert held == {"kind": "clear_hour", "clear_hour": 9, "assembly_indoors": True, "limit_outdoor": False}
 
 
+def test_a_fixed_hour_replaces_the_forecast_pick_on_stale_mornings():
+    hourly = _hours(300, 260, 220, 180, 150, 160)  # the forecast's cleanest is 12:00
+    assert decide(hourly)["clear_hour"] == 12
+    d = decide(hourly, fixed_hour=13)
+    assert d["clear_hour"] == 13 and d["limit_outdoor"] is True
+    assert decide(_hours(60, 70, 80, 90, 85, 75), fixed_hour=13)["kind"] == "fine"  # fine days name no hour
+
+
 def test_keep_short_advice_when_even_the_clear_hour_is_very_poor():
     d = decide(_hours(300, 260, 220, 180, 150, 160))
     en = message_params(d, "KV RK Puram", date(2025, 11, 13), "en")
```

### `/tmp/page.patch` (Verbatim)

```diff
--- a/site/index.html
+++ b/site/index.html
@@ -75,14 +75,16 @@
 }
 .is-replay .replay-note { display: block; }
 .dateline { color: var(--ink-2); font-size: 14px; margin: 0 0 4px; }
+.stale-note { color: var(--ink-2); font-size: 14px; margin: 0 0 8px; padding-left: 10px; border-left: 3px solid var(--hairline); }
 .headline { font-size: 25px; line-height: 1.22; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 18px; }
-.headline .slot { color: var(--clear); white-space: nowrap; }
+.headline .slot { white-space: nowrap; text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 4px; }
 
 .strip-title { font-size: 13px; color: var(--ink-2); margin: 0 0 6px; }
 .strip { width: 100%; height: auto; display: block; overflow: visible; }
 .strip text { font-family: var(--font); }
 .strip .v { font-size: 12px; fill: var(--ink-2); }
-.strip .v.on { fill: var(--clear); font-weight: 700; }
+.strip .v.on { fill: var(--ink); font-weight: 700; }
+.strip .t.on { fill: var(--ink); font-weight: 600; }
 .strip .t { font-size: 12px; fill: var(--muted); }
 .strip .ref { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; }
 .strip .reflabel { font-size: 11px; fill: var(--muted); }
@@ -198,6 +200,7 @@
     <aside class="brief">
       <p class="replay-note" id="replay-note"></p>
       <p class="dateline" id="dateline"></p>
+      <p class="stale-note" id="stale-note" hidden></p>
       <h1 class="headline" id="headline"></h1>
       <p class="strip-title" id="city-strip-title"></p>
       <svg class="strip" id="city-strip" role="img"></svg>
@@ -236,6 +239,8 @@
     place: "Delhi schools", today: "Today", replay: (d) => `Replay ${d}`,
     replayNote: (d) => `Replay of ${d}, run on that morning's data. This is not today's air.`,
     forecastFor: (d, t) => `Forecast for ${d}, made at ${t}`,
+    readingsUpTo: (w) => `. Monitor readings available up to ${w}`,
+    staleNote: (slot) => `The night's readings haven't arrived yet, so each school's Clear Hour is ${slot}, the school hour that's cleanest most often.`,
     none: "No forecast yet. The first run is at 5:30 AM IST.",
     allFine: (n) => `Air is fine at all ${n} schools today.`,
     fine: (k, n) => `Air is fine at ${k} of ${n} schools today.`,
@@ -268,6 +273,8 @@
     place: "दिल्ली के स्कूल", today: "आज", replay: (d) => `रीप्ले ${d}`,
     replayNote: (d) => `${d} की सुबह का रीप्ले, उसी सुबह के डेटा से। यह आज की हवा नहीं है।`,
     forecastFor: (d, t) => `${d} का पूर्वानुमान, ${t} पर बना`,
+    readingsUpTo: (w) => `। मॉनिटर रीडिंग ${w} तक उपलब्ध`,
+    staleNote: (slot) => `रात की रीडिंग अभी नहीं आईं, इसलिए हर स्कूल का साफ़ समय ${slot} है, जो अक्सर सबसे साफ़ घंटा होता है।`,
     none: "अभी कोई पूर्वानुमान नहीं है। पहला रन सुबह 5:30 बजे होगा।",
     allFine: (n) => `आज सभी ${n} स्कूलों में हवा ठीक है।`,
     fine: (k, n) => `आज ${n} में से ${k} स्कूलों में हवा ठीक है।`,
@@ -359,11 +366,11 @@
   HOURS.forEach((h, i) => {
     const v = values[i], x = x0 + i * bw + gap / 2, w = bw - gap, on = h === clearHour;
     const yy = v == null ? y(0) : y(v), r = Math.min(4, w / 2);
-    if (v != null) out += `<path d="M${x},${y(0)} V${yy + r} Q${x},${yy} ${x + r},${yy} H${x + w - r} Q${x + w},${yy} ${x + w},${yy + r} V${y(0)} Z" fill="${on ? "var(--clear)" : "var(--bar)"}"><title>${slotLabel(h, state.lang)}: ${Math.round(v)} µg/m³</title></path>`;
+    if (v != null) out += `<path d="M${x},${y(0)} V${yy + r} Q${x},${yy} ${x + r},${yy} H${x + w - r} Q${x + w},${yy} ${x + w},${yy + r} V${y(0)} Z" fill="${on ? "var(--ink)" : "var(--bar)"}"><title>${slotLabel(h, state.lang)}: ${Math.round(v)} µg/m³</title></path>`;
     out += `<text class="v${on ? " on" : ""}" x="${x + w / 2}" y="${yy - 5}" text-anchor="middle">${v == null ? "–" : Math.round(v)}</text>`;
     const [hh, ap] = clock(h);
     const lab = i === 0 || i === HOURS.length - 1 ? `${hh} ${state.lang === "hi" ? (h < 12 ? "सुबह" : "दोपहर") : ap}` : `${hh}`;
-    out += `<text class="t" x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${lab}</text>`;
+    out += `<text class="t${on ? " on" : ""}" x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${lab}</text>`;
   });
   svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
   svg.innerHTML = out;
@@ -382,12 +389,21 @@
   $("key-monitor").textContent = L.keyMonitor;
   $("key-demo").textContent = L.keyDemo;
   if (!d) {
-    $("dateline").textContent = ""; $("headline").textContent = L.none;
+    $("dateline").textContent = ""; $("stale-note").hidden = true; $("headline").textContent = L.none;
     $("city-strip").innerHTML = ""; $("city-strip-title").textContent = ""; $("legend").innerHTML = ""; $("pilots").innerHTML = "";
     return;
   }
   $("replay-note").textContent = L.replayNote(longDay(d.day, state.lang));
-  $("dateline").textContent = L.forecastFor(longDay(d.day, state.lang), timeIST(d.generated_at));
+  let dateline = L.forecastFor(longDay(d.day, state.lang), timeIST(d.generated_at));
+  if (d.obs_through && new Date(d.obs_through) < new Date(`${d.day}T04:00:00+05:30`)) {
+    const through = new Date(d.obs_through);
+    const iso = new Date(through.getTime() + 5.5 * 3600e3).toISOString().slice(0, 10);
+    dateline += L.readingsUpTo(`${dayLabel(iso, state.lang)}, ${timeIST(d.obs_through)}`);
+  }
+  $("dateline").textContent = dateline;
+  const staleNote = $("stale-note");
+  staleNote.hidden = d.stale_hour == null;
+  staleNote.textContent = d.stale_hour == null ? "" : L.staleNote(slotLabel(d.stale_hour, state.lang));
   const n = d.schools.length;
   const counts = [0, 0, 0, 0, 0];
   const clearCounts = {};
@@ -469,6 +485,7 @@
     <div class="bubble">${esc(L.msg(params))}${meta ? `<span class="meta">${esc(meta)}</span>` : ""}</div>
     <p class="label">${esc(L.stripSchool)}</p>
     <svg class="strip" id="school-strip" role="img" aria-label="${esc(L.stripSchool)}"></svg>
+    ${d.stale_hour != null && s.kind === "clear_hour" ? `<p class="latest">${esc(L.staleNote(slotLabel(d.stale_hour, state.lang)))}</p>` : ""}
     ${s.latest != null ? `<p class="latest">${L.latest(Math.round(s.latest))}</p>` : ""}
     ${a ? `<p class="demo">${esc(L.demo)}</p>` : ""}`;
   drawStrip($("school-strip"), s.hourly, s.clear);
```
