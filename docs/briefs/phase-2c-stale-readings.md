# Phase 2c brief: forecast from late readings

Read `CLAUDE.md` first. Do S1 → S4 in order: check, commit, then move to the next. Stop after S4 and report
with the block at the end. The redeploy in S4 is approved, with `WA_MODE` staying `dry_run`.

## The decision

OpenAQ receives Delhi's CPCB readings in late batches. At 00:54 IST today the newest was from 2 Oct; at
12:10 it was 17 hours old. Nothing guarantees the 04:00 reading by 05:30 on any day.

We're not switching sources:
- data.gov.in's real-time feed reports a min, max and average per pollutant. Its sample rows look like 24-hour
  figures, not the hourly readings the features need.
- Scraping CPCB's dashboard would be fragile.

Instead, the model learns to forecast from whatever has arrived:
- **Training adds blackouts.** Each day is rebuilt as if the last 0, 6, 12, …, 36 hours of readings before
  04:00 hadn't been published yet. Targets don't change, and `lead_h` tells the model how old its newest
  reading is.
- **At 05:30, the forecast measures the blackout from the data** and builds features to match. Up to 36 hours
  it forecasts; beyond that, the run fails loudly as it does now. A model trained without blackouts refuses any
  blackout above zero, so the old model can never be fed stale readings by mistake.
- **The assembly call uses the latest reading only when it's from the night** (an 08:00 lead of 10 hours or
  less). Otherwise it falls back to the 08:00 forecast, which was our rule before the addendum.
- **The page says how fresh the readings were.**
- **The page marks the Clear Hour in dark ink, not blue.** Blue means clean air, and on a winter morning the
  Clear Hour is often very poor. Your design note was right.

The backtest by blackout shows how much accuracy a late morning costs. That table goes in the README.

---

## S1: Code (Verbatim patch)

Save the patch below to `/tmp/stale.patch`. Run `git apply --check /tmp/stale.patch`, then `git apply`. If a
hunk fails, make the same change by hand. Keep the off-hour timestamp guard you added to `build_rows` in
Phase 2; the patch doesn't touch it.

What it changes:
- `features.py`: adds `BLACKOUTS_H`, `MAX_BLACKOUT_H`, `FRESH_LEAD_H`, `blackout_for()` and
  `build_rows(..., blackout_h=0)`.
- Forecast handler:
  - measures the blackout, and refuses one beyond the model's `blackouts_h` in `features.json`
  - keeps `latest` only for stations with a reading from the night
  - adds `blackout_h` and `obs_through` to `forecast.json`
- `site.py`: adds `obs_through` to the day file.
- Tests: a blackout unit test, and a pipeline test of a stale morning.

**Check:** `uv run pytest -q` (25 tests) and ruff clean.
Commit: `core: forecast from late readings`

### `/tmp/stale.patch` (Verbatim)

```diff
--- a/src/clearhour/features.py
+++ b/src/clearhour/features.py
@@ -8,10 +8,15 @@
 import numpy as np
 import pandas as pd
 
-from clearhour.constants import IST, TARGET_HOURS
+from clearhour.constants import ASSEMBLY_HOUR, IST, TARGET_HOURS
 
-LATEST_OBS_HOUR = 4  # newest bin assumed published by 05:30 IST (the 04:00-05:00 hour); tune from live ingest
-MAX_STALENESS_H = 6  # if that bin is missing, fall back at most this many hours
+LATEST_OBS_HOUR = 4  # the 04:00-05:00 IST bin: the newest one that could exist at 05:30
+MAX_STALENESS_H = 6  # a station may lag the cutoff by at most this many hours
+# OpenAQ publishes Delhi's readings in late batches, so at 05:30 the newest reading can be a day old. The model
+# trains on these blackouts (hours of missing readings before 04:00) and serves whichever one the data shows.
+BLACKOUTS_H = (0, 6, 12, 18, 24, 30, 36)
+MAX_BLACKOUT_H = BLACKOUTS_H[-1]
+FRESH_LEAD_H = ASSEMBLY_HOUR - LATEST_OBS_HOUR + MAX_STALENESS_H  # 08:00 leads up to this: a reading from the night
 MET_COLUMNS = ["pm2_5", "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "boundary_layer_height"]
 
 FEATURES = [
@@ -39,12 +44,23 @@
     return float(a.mean()) if a.size else float("nan")
 
 
-def build_rows(hourly: pd.DataFrame, met: pd.DataFrame, days) -> pd.DataFrame:
+def blackout_for(hourly: pd.DataFrame, day) -> int:
+    """Hours between the 04:00 IST bin and the newest reading at or before it (0 when that bin is in)."""
+    cutoff = pd.Timestamp(day).tz_convert(IST).normalize() + pd.Timedelta(hours=LATEST_OBS_HOUR)
+    seen = hourly.loc[hourly["hour_ist"] <= cutoff, "hour_ist"]
+    if seen.empty:
+        return MAX_BLACKOUT_H + 1
+    return int((cutoff - seen.max()) / pd.Timedelta(hours=1))
+
+
+def build_rows(hourly: pd.DataFrame, met: pd.DataFrame, days, blackout_h: int = 0) -> pd.DataFrame:
     """Feature rows for each station x day x school hour.
 
     hourly: columns location_id, hour_ist (tz-aware, IST, hour-beginning), pm25.
     met: city-point hourly weather and CAMS indexed by tz-aware hour, with MET_COLUMNS.
     days: IST midnights (tz-aware) to build rows for. `target` is NaN where no reading exists.
+    blackout_h: readings from the last this-many hours before 04:00 count as not yet published, for the
+    latest-reading and yesterday features alike. Targets are never affected.
     """
     if hourly.empty:
         return pd.DataFrame(columns=["day", "target", *FEATURES])
@@ -62,16 +78,18 @@
     for day in days:
         d0 = pd.Timestamp(day).tz_convert(IST).normalize()
         i_latest = pos.get(d0 + pd.Timedelta(hours=LATEST_OBS_HOUR))
-        if i_latest is None:
+        if i_latest is None or i_latest - blackout_h < 0:
             continue
+        i_cut = i_latest - blackout_h  # the newest hour treated as published
         last_idx = np.full(len(stations), -1)
         for k in range(MAX_STALENESS_H + 1):
-            i = i_latest - k
+            i = i_cut - k
             if i < 0:
                 break
             hit = (last_idx < 0) & np.isfinite(vals[i])
             last_idx[hit] = i
         y_idx = [pos.get(d0 - pd.Timedelta(days=1) + pd.Timedelta(hours=t)) for t in TARGET_HOURS]
+        y_idx = [i if i is not None and i <= i_cut else None for i in y_idx]
         t_idx = [pos.get(d0 + pd.Timedelta(hours=t)) for t in TARGET_HOURS]
         for j, station in enumerate(stations):
             li = int(last_idx[j])
--- a/src/clearhour/handlers/forecast.py
+++ b/src/clearhour/handlers/forecast.py
@@ -71,21 +71,28 @@
         raise RuntimeError("model was trained on a different feature list; retrain")
     hourly = live_obs(day) if source == "live" else archive_obs(day)
     met = meteo.fetch((day - pd.Timedelta(days=1)).date().isoformat(), day.date().isoformat(), live=source == "live")
-    rows = features.build_rows(hourly, met, [day])
+    blackout = features.blackout_for(hourly, day)
+    trained_up_to = max(meta.get("blackouts_h", [0]))
+    if blackout > trained_up_to:
+        raise RuntimeError(f"the newest reading is {blackout} h before 04:00 IST; the model covers {trained_up_to} h")
+    rows = features.build_rows(hourly, met, [day], blackout_h=blackout)
     if rows.empty:
-        raise RuntimeError(f"no station had a reading within {features.MAX_STALENESS_H} h of the 04:00 IST bin")
+        raise RuntimeError(f"no station had a reading within {features.MAX_STALENESS_H} h of the newest one")
     rows["location_id"] = pd.Categorical(rows["location_id"], categories=meta["stations"])
     rows["pred"] = np.expm1(booster.predict(rows[features.FEATURES]))
     preds: dict[str, dict[str, float]] = {}
-    latest: dict[str, float] = {}  # each station's newest reading at or before 04:00 IST (v_last)
+    latest: dict[str, float] = {}  # stations with a reading from the night; the assembly call uses only these
     for r in rows.itertuples():
         preds.setdefault(str(r.location_id), {})[str(r.target_hour)] = round(float(r.pred), 1)
-        latest[str(r.location_id)] = round(float(r.v_last), 1)
+        if r.target_hour == 8 and r.lead_h <= features.FRESH_LEAD_H:
+            latest[str(r.location_id)] = round(float(r.v_last), 1)
     body = {
         "day": str(day.date()),
         "source": source,
         "generated_at": pd.Timestamp.now(tz=IST).isoformat(timespec="seconds"),
         "median_lead_h": float(rows["lead_h"].median()),
+        "blackout_h": blackout,
+        "obs_through": (day + pd.Timedelta(hours=features.LATEST_OBS_HOUR - blackout)).isoformat(),
         "stations": preds,
         "latest": latest,
     }
@@ -95,7 +102,13 @@
     )
     print(
         json.dumps(
-            {"day": body["day"], "source": source, "stations": len(preds), "median_lead_h": body["median_lead_h"]}
+            {
+                "day": body["day"],
+                "source": source,
+                "stations": len(preds),
+                "blackout_h": blackout,
+                "median_lead_h": body["median_lead_h"],
+            }
         )
     )
     return {
--- a/src/clearhour/site.py
+++ b/src/clearhour/site.py
@@ -70,6 +70,7 @@
         "source": fc["source"],
         "generated_at": fc["generated_at"],
         "median_lead_h": fc.get("median_lead_h"),
+        "obs_through": fc.get("obs_through"),
         "hours": TARGET_HOURS,
         "stations": [
             [s["location_id"], s["name"], s["lat"], s["lon"], latest.get(str(s["location_id"]))] for s in stations()
--- a/tests/test_model.py
+++ b/tests/test_model.py
@@ -1,7 +1,7 @@
 import pandas as pd
 from synthetic import make_synthetic
 
-from clearhour.features import FEATURES, MET_COLUMNS, build_rows
+from clearhour.features import FEATURES, MET_COLUMNS, blackout_for, build_rows
 from clearhour.model import evaluate, walk_forward, with_station_category
 
 IST = "Asia/Kolkata"
@@ -35,3 +35,18 @@
     assert res["station_days"] > 100
     assert 0 <= res["hit_rate_top2"]["model"] <= 1
     assert res["realised_cut_vs_assembly"]["median"] > 0
+
+
+def test_blackout_hides_late_readings_from_features_but_not_targets():
+    hourly, met = make_synthetic(n_stations=2, end="2025-11-01")
+    day = pd.Timestamp("2025-10-20", tz=IST)
+    fresh = build_rows(hourly, met, [day])
+    stale = build_rows(hourly, met, [day], blackout_h=24)
+    assert not stale.empty and (stale["lead_h"] >= 4 + 24).all()
+    assert stale["v_yday_t"].isna().all()  # yesterday's school hours all fall after 04:00 the day before
+    key = ["location_id", "target_hour"]
+    both = fresh.merge(stale, on=key, suffixes=("_f", "_s"))
+    assert (both["target_f"].fillna(-1) == both["target_s"].fillna(-1)).all()
+    upto = hourly[hourly["hour_ist"] <= pd.Timestamp("2025-10-19 11:00", tz=IST)]
+    assert blackout_for(upto, day) == 17
+    assert blackout_for(hourly, day) == 0
--- a/tests/test_pipeline.py
+++ b/tests/test_pipeline.py
@@ -52,13 +52,16 @@
         yield
 
 
-def _seed_model_and_obs(monkeypatch, day: pd.Timestamp):
+def _seed_model_and_obs(monkeypatch, day: pd.Timestamp, obs_until: pd.Timestamp | None = None):
     from clearhour.handlers import forecast
 
     hourly, met = make_synthetic(n_stations=3, start="2025-10-01", end="2025-11-20")
     stations = [int(s) for s in sorted(hourly["location_id"].unique())]  # plain ints: they go into JSON
     train_days = pd.date_range("2025-10-03", "2025-11-10", tz="Asia/Kolkata")
-    rows = with_station_category(features.build_rows(hourly, met, train_days), stations)
+    rows = pd.concat(
+        [with_station_category(features.build_rows(hourly, met, train_days, blackout_h=b), stations) for b in (0, 24)],
+        ignore_index=True,
+    )
     assert not rows.empty
     booster = fit(rows, num_rounds=30)
     s3 = boto3.client("s3")
@@ -66,11 +69,10 @@
     s3.put_object(
         Bucket=BUCKET,
         Key="models/features.json",
-        Body=json.dumps({"features": features.FEATURES, "stations": stations}).encode(),
+        Body=json.dumps({"features": features.FEATURES, "stations": stations, "blackouts_h": [0, 24]}).encode(),
     )
-    recent = hourly[
-        (hourly["hour_ist"] >= day - pd.Timedelta(days=2)) & (hourly["hour_ist"] < day + pd.Timedelta(hours=5))
-    ]
+    until = obs_until if obs_until is not None else day + pd.Timedelta(hours=5)
+    recent = hourly[(hourly["hour_ist"] >= day - pd.Timedelta(days=2)) & (hourly["hour_ist"] < until)]
     for r in recent.itertuples():
         store.put_obs(int(r.location_id), r.hour_ist.tz_convert("UTC").isoformat(), float(r.pm25), 4)
     monkeypatch.setattr(forecast, "_s3", s3)
@@ -124,3 +126,17 @@
     assert store.get_alert("node/1", today)["acted"] is True
     alerts = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key="site/data/alerts.json")["Body"].read())
     assert alerts["days"][today][0]["acted"] is True
+
+
+def test_stale_readings_still_forecast_but_leave_assembly_to_the_forecast(aws, monkeypatch):
+    from clearhour.handlers import decide
+
+    day = pd.Timestamp("2025-11-13", tz="Asia/Kolkata")
+    forecast = _seed_model_and_obs(monkeypatch, day, obs_until=day - pd.Timedelta(hours=12))  # newest: 11:00 yesterday
+    out = forecast.handler({"source": "live", "as_of": "2025-11-13T05:30:00+05:30"}, None)
+    fc = json.loads(boto3.client("s3").get_object(Bucket=BUCKET, Key=out["forecast_key"])["Body"].read())
+    assert fc["blackout_h"] == 17 and fc["obs_through"].startswith("2025-11-12T11:00")
+    assert fc["latest"] == {}  # nothing from the night, so no reading drives the assembly call
+    monkeypatch.setattr(decide, "_s3", boto3.client("s3"))
+    monkeypatch.setattr(decide, "schools", lambda: SCHOOLS)
+    assert decide.handler(out, None)["schools"] == 1
```

---

## S2: Backtest by blackout

In `scripts/backtest.py`:

1. **Build rows for every blackout**, each tagged with its own:

   ```python
   from clearhour.features import BLACKOUTS_H

   rows = pd.concat(
       [
           with_station_category(build_rows(hourly, met, days, blackout_h=b), stations).assign(blackout_h=b)
           for b in BLACKOUTS_H
       ],
       ignore_index=True,
   )
   ```

2. **Fold both backtests over all blackouts.** The winter walk-forward and the October fold run as before, so
   training sees every blackout. Score each blackout separately with `evaluate()` and `decision_scores()`.
3. **Replace `school_day_matrix` and `rule_decisions`** with the Verbatim versions below, and import
   `FRESH_LEAD_H` from `clearhour.features`. Scoring now treats the latest reading exactly as production does.
4. **Keep the blackout-0 results at the top level of `backtest.json`**, since those are the README's numbers.
   Add the rest under `"by_blackout"`. Run the asserts against `evaluate()` on the blackout-0 subset.
5. **Print, for winter, one row per blackout:**
   - MAE for the model, persistence, CAMS and yesterday
   - the `clearhour` hit rate, median cut, pooled cut and worse-than-assembly share

   Print the assembly-flag table for blackout 0 only.

**Stop and show me before S3** if, at blackout 0, the `clearhour` pooled cut falls more than 0.010 below
0.250, or the model's MAE rises above 52.0. Either would mean the blackouts are costing us fresh mornings.
Commit: `analysis: backtest by reading blackout`

### `school_day_matrix` and `rule_decisions` (Verbatim)

```python
def school_day_matrix(pred: pd.DataFrame) -> dict[str, np.ndarray]:
    """The station-days evaluate() scores (Mon-Fri, all six school hours observed), as arrays with one row per
    station-day and one column per school hour 08..13: the actual value, the model forecast and CAMS.
    "v_last" and "lead_h" are one value per station-day: the latest reading the forecast started from, and the
    08:00 lead from it."""
    cols = ["target", "pred", "cams_t", "v_last", "lead_h"]
    ok = pred[pred["target"].notna() & (pred["day"].dt.dayofweek < 5)]
    size = ok.groupby(["location_id", "day"], observed=True)["target"].transform("size")
    full = ok[size == len(TARGET_HOURS)]
    wide = full.set_index(["location_id", "day", "target_hour"])[cols].unstack("target_hour")
    m = {c: wide[c][TARGET_HOURS].to_numpy(dtype=float) for c in cols}
    m["v_last"] = m["v_last"][:, ASSEMBLY_COL]
    m["lead_h"] = m["lead_h"][:, ASSEMBLY_COL]
    return m


def rule_decisions(m: dict[str, np.ndarray], use_latest: bool = True) -> list[dict]:
    """The production rule (clearhour.decide) on each station-day's forecast. As in production, the latest
    reading makes the assembly call only when it's from the night (08:00 lead up to FRESH_LEAD_H)."""
    return [
        decide(
            dict(zip(TARGET_HOURS, row, strict=True)),
            latest=float(now) if use_latest and lead <= FRESH_LEAD_H else None,
        )
        for row, now, lead in zip(m["pred"], m["v_last"], m["lead_h"], strict=True)
    ]
```

---

## S3: Retrain, then redeploy with the page change

1. **Retrain.** `train_final.py` trains on the same all-blackout rows, through the parquet's last full day.
   `features.json` gains `"blackouts_h": list(BLACKOUTS_H)`. Print the row counts per blackout and the top 8
   features by gain.
   Commit: `model: retrain on reading blackouts`
2. **Page.** Save the patch below to `/tmp/page.patch` and apply it the same way. It marks the Clear Hour in ink
   and adds the freshness line.
3. **Deploy.** Run `./scripts/deploy.sh`, which uploads the new model and the page.
4. **Live dry run.** Start a run with `{"source": "live", "resend": true}`. Expect SUCCEEDED, then report:
   - from `forecast.json`: `blackout_h`, `obs_through` and `median_lead_h`
   - the four params from the Send log
5. **Check the page.** Open it and confirm that Today now has data and the dateline says how fresh the
   readings were.

Commit: `dashboard: Clear Hour in ink, reading freshness`

### `/tmp/page.patch` (Verbatim)

```diff
--- a/site/index.html
+++ b/site/index.html
@@ -76,13 +76,14 @@
 .is-replay .replay-note { display: block; }
 .dateline { color: var(--ink-2); font-size: 14px; margin: 0 0 4px; }
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
@@ -236,6 +237,7 @@
     place: "Delhi schools", today: "Today", replay: (d) => `Replay ${d}`,
     replayNote: (d) => `Replay of ${d}, run on that morning's data. This is not today's air.`,
     forecastFor: (d, t) => `Forecast for ${d}, made at ${t}`,
+    readingsUpTo: (w) => `. Monitor readings available up to ${w}`,
     none: "No forecast yet. The first run is at 5:30 AM IST.",
     allFine: (n) => `Air is fine at all ${n} schools today.`,
     fine: (k, n) => `Air is fine at ${k} of ${n} schools today.`,
@@ -268,6 +270,7 @@
     place: "दिल्ली के स्कूल", today: "आज", replay: (d) => `रीप्ले ${d}`,
     replayNote: (d) => `${d} की सुबह का रीप्ले, उसी सुबह के डेटा से। यह आज की हवा नहीं है।`,
     forecastFor: (d, t) => `${d} का पूर्वानुमान, ${t} पर बना`,
+    readingsUpTo: (w) => `। मॉनिटर रीडिंग ${w} तक उपलब्ध`,
     none: "अभी कोई पूर्वानुमान नहीं है। पहला रन सुबह 5:30 बजे होगा।",
     allFine: (n) => `आज सभी ${n} स्कूलों में हवा ठीक है।`,
     fine: (k, n) => `आज ${n} में से ${k} स्कूलों में हवा ठीक है।`,
@@ -359,11 +362,11 @@
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
@@ -387,7 +390,13 @@
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
   const n = d.schools.length;
   const counts = [0, 0, 0, 0, 0];
   const clearCounts = {};
```

## S4: Stop and report

Tomorrow's 05:30 run will use the new model. Its `blackout_h` is our first real-world measurement, so the
next session should start by reading it.

```
PHASE 2C REPORT
S1: <n> tests; ruff ok
S2 winter, by blackout (h): MAE model/persist/CAMS/yday | hit | median cut | pooled cut | worse
   0: ...
   6: ...
  12: ...
  18: ...
  24: ...
  30: ...
  36: ...
  assembly flag at 0: model <acc> · persistence <acc>
S3: rows per blackout <n>; trained_through <date>; top features <...>
    deploy <ok>; live run <status>; blackout <h> h; obs_through <IST>; median lead <h>; params <4 values>
    page: Today <ok>; freshness line <text>
Commits: <git log --oneline -5>
```
