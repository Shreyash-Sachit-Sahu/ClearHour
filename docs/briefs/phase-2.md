# Phase 2 brief: model, live pipeline, first real message

Read `CLAUDE.md` first; its rules apply to every task here.

**Before you start:** Phase 1 (T6–T8) is done, and:
- Docker Desktop is running with WSL integration, so `docker info` works inside WSL.
- The `clearhour` AWS profile works.
- The WhatsApp number is Active and the template is approved, or we use Plan B or dry run for now.

**Gate:** a Step Functions run of the daily pipeline puts a real WhatsApp alert on Shreyash's phone, and
replying "1" marks it as acted on. Until WhatsApp is ready, the same run in dry-run mode logs the exact
message payload.

## Ground rules for this phase

- The Phase 1 rules still apply: do the tasks in order, then check, commit, stop and show me.
- The **Verbatim** files passed these checks before handover:
  - `ruff check` and `ruff format --check` (line length 120)
  - 10 tests, including one that runs forecast → decide → send → reply end to end on mocked AWS (moto)
  - cfn-lint on the template
- Untested here, so your task checks are the real test:
  - live calls to Open-Meteo and the OpenAQ API
  - the Docker image build
  - End User Messaging Social
  - anything deployed
- `tests/` must not contain an `__init__.py`, because the tests import the shared `tests/synthetic.py` directly.

---

## P2-T0: CLAUDE.md and .env.example

Add under **Stack and versions** in `CLAUDE.md`:

```
- Lambda packaging: zip functions use CodeUri `src/` and may import only the standard library, boto3 and the
  dependency-free modules (`constants`, `decide`, `store`, `whatsapp`). Anything that needs pandas or LightGBM
  runs in the Forecast container image.
```

Add under **Commands**:

```
./scripts/deploy.sh      # never `sam deploy --guided` again: it saves parameter values, including the API key, into samconfig.toml
```

Add these lines to `.env.example`, values empty:

```
WA_MODE=dry_run
WA_PHONE_NUMBER_ID=
WA_EVENTS_TOPIC_ARN=
WA_TEMPLATE_LANG_EN=
WA_TEMPLATE_LANG_HI=
PILOT_PHONES=
PILOT_LANGS=
META_PHONE_NUMBER_ID=
META_ACCESS_TOKEN=
```

Commit: `docs: Phase 2 conventions`.

## P2-T1: Library, handlers and tests (Verbatim)

Run `uv add --dev "moto[dynamodb,s3]"`, then create the files below exactly. `src/clearhour/handlers/__init__.py`
is an empty file.

### `src/clearhour/constants.py` (Verbatim)

```python
"""Dependency-free constants, safe to import from the zip Lambdas (no pandas there)."""

IST = "Asia/Kolkata"
TARGET_HOURS = list(range(8, 14))  # hour-beginning bins, 08:00 to 13:00 IST
ASSEMBLY_HOUR = TARGET_HOURS[0]
```

### `src/clearhour/features.py` (Verbatim)

The same feature code serves the backtest, the final training run and the Forecast Lambda, so training and
serving can't drift apart.

```python
"""Model rows for the 05:30 IST forecast: one row per station, day and school hour (08:00-13:00 IST).

Shared by the backtest, the final training run and the Forecast Lambda, so train and serve can't drift.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from clearhour.constants import IST, TARGET_HOURS

LATEST_OBS_HOUR = 4  # newest bin assumed published by 05:30 IST (the 04:00-05:00 hour); tune from live ingest
MAX_STALENESS_H = 6  # if that bin is missing, fall back at most this many hours
MET_COLUMNS = ["pm2_5", "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "boundary_layer_height"]

FEATURES = [
    "location_id",
    "target_hour",
    "lead_h",
    "dow",
    "v_last",
    "v_last3",
    "v_yday_t",
    "v_yday_school",
    "cams_t",
    "cams_last",
    "temp_t",
    "rh_t",
    "wind_t",
    "blh_t",
    "blh_last",
    "blh_ratio",
]


def _nanmean(a: np.ndarray) -> float:
    a = a[np.isfinite(a)]
    return float(a.mean()) if a.size else float("nan")


def build_rows(hourly: pd.DataFrame, met: pd.DataFrame, days) -> pd.DataFrame:
    """Feature rows for each station x day x school hour.

    hourly: columns location_id, hour_ist (tz-aware, IST, hour-beginning), pm25.
    met: city-point hourly weather and CAMS indexed by tz-aware hour, with MET_COLUMNS.
    days: IST midnights (tz-aware) to build rows for. `target` is NaN where no reading exists.
    """
    if hourly.empty:
        return pd.DataFrame(columns=["day", "target", *FEATURES])
    wide = hourly.pivot_table(index="hour_ist", columns="location_id", values="pm25", aggfunc="mean")
    start = min(wide.index.min(), *days) - pd.Timedelta(days=2)
    end = max(wide.index.max(), *days) + pd.Timedelta(days=1)
    full = pd.date_range(start.floor("D"), end.ceil("D"), freq="h", tz=IST)
    vals = wide.reindex(full).to_numpy(dtype=float)
    metv = met.reindex(full.tz_convert(met.index.tz) if met.index.tz is not None else full)[MET_COLUMNS]
    m = {c: metv[c].to_numpy(dtype=float) for c in MET_COLUMNS}
    pos = {ts: i for i, ts in enumerate(full)}
    stations = wide.columns.to_numpy()

    rows: list[dict] = []
    for day in days:
        d0 = pd.Timestamp(day).tz_convert(IST).normalize()
        i_latest = pos.get(d0 + pd.Timedelta(hours=LATEST_OBS_HOUR))
        if i_latest is None:
            continue
        last_idx = np.full(len(stations), -1)
        for k in range(MAX_STALENESS_H + 1):
            i = i_latest - k
            if i < 0:
                break
            hit = (last_idx < 0) & np.isfinite(vals[i])
            last_idx[hit] = i
        y_idx = [pos.get(d0 - pd.Timedelta(days=1) + pd.Timedelta(hours=t)) for t in TARGET_HOURS]
        t_idx = [pos.get(d0 + pd.Timedelta(hours=t)) for t in TARGET_HOURS]
        for j, station in enumerate(stations):
            li = int(last_idx[j])
            if li < 0:
                continue
            col = vals[:, j]
            yday = np.array([col[i] if i is not None else np.nan for i in y_idx])
            v_yday_school = _nanmean(yday)
            v_last3 = _nanmean(col[max(li - 2, 0) : li + 1])
            blh_last = m["boundary_layer_height"][li]
            for t, yv, it in zip(TARGET_HOURS, yday, t_idx, strict=True):
                if it is None:
                    continue
                blh_t = m["boundary_layer_height"][it]
                rows.append(
                    {
                        "day": d0,
                        "target": col[it],
                        "location_id": station,
                        "target_hour": t,
                        "lead_h": it - li,
                        "dow": d0.dayofweek,
                        "v_last": col[li],
                        "v_last3": v_last3,
                        "v_yday_t": yv,
                        "v_yday_school": v_yday_school,
                        "cams_t": m["pm2_5"][it],
                        "cams_last": m["pm2_5"][li],
                        "temp_t": m["temperature_2m"][it],
                        "rh_t": m["relative_humidity_2m"][it],
                        "wind_t": m["wind_speed_10m"][it],
                        "blh_t": blh_t,
                        "blh_last": blh_last,
                        "blh_ratio": blh_t / max(blh_last, 50.0)
                        if np.isfinite(blh_t) and np.isfinite(blh_last)
                        else np.nan,
                    }
                )
    return pd.DataFrame(rows, columns=["day", "target", *FEATURES])
```

### `src/clearhour/model.py` (Verbatim)

```python
"""LightGBM training, walk-forward backtest and the decision metrics."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd

from clearhour.constants import TARGET_HOURS
from clearhour.features import FEATURES

PARAMS = {
    "objective": "regression",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 40,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.9,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "seed": 7,
    "deterministic": True,
    "force_row_wise": True,
    "verbose": -1,
}
NUM_ROUNDS = 400


def with_station_category(rows: pd.DataFrame, stations: list) -> pd.DataFrame:
    """Fix the station categories so codes match across folds and at serving time."""
    return rows.assign(location_id=pd.Categorical(rows["location_id"], categories=stations))


def fit(train: pd.DataFrame, num_rounds: int = NUM_ROUNDS) -> lgb.Booster:
    train = train[train["target"].notna()]
    data = lgb.Dataset(train[FEATURES], label=np.log1p(train["target"]), categorical_feature=["location_id"])
    return lgb.train(PARAMS, data, num_boost_round=num_rounds)


def predict(model: lgb.Booster, rows: pd.DataFrame) -> np.ndarray:
    return np.expm1(model.predict(rows[FEATURES]))


def walk_forward(rows: pd.DataFrame, week_starts: list[pd.Timestamp], num_rounds: int = NUM_ROUNDS) -> pd.DataFrame:
    """For each test week: train on every earlier day, predict that week. Returns the test rows with `pred`."""
    out = []
    for wk in week_starts:
        train = rows[rows["day"] < wk]
        test = rows[(rows["day"] >= wk) & (rows["day"] < wk + pd.Timedelta(days=7))]
        if train["target"].notna().sum() == 0 or test.empty:
            continue
        model = fit(train, num_rounds)
        out.append(test.assign(pred=predict(model, test), week=wk))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def evaluate(pred: pd.DataFrame) -> dict:
    """Forecast error and decision quality on school days (Mon-Fri) with all six school hours observed."""
    ok = pred[pred["target"].notna()]

    def mae(col: str) -> float:
        valid = ok[ok[col].notna()]
        return round(float((valid[col] - valid["target"]).abs().mean()), 1)

    result = {
        "rows": int(len(ok)),
        "mae": {
            "model": mae("pred"),
            "persistence": mae("v_last"),
            "cams": mae("cams_t"),
            "yesterday": mae("v_yday_t"),
        },
    }

    days = ok[ok["day"].dt.dayofweek < 5]
    full = days.groupby(["location_id", "day"], observed=True).filter(lambda g: len(g) == len(TARGET_HOURS))
    hits = {"model": [], "cams": [], "always_12": [], "always_13": []}
    cuts: list[float] = []
    for _, g in full.groupby(["location_id", "day"], observed=True):
        g = g.set_index("target_hour")
        best_two = set(g["target"].nsmallest(2).index)
        hits["model"].append(int(g["pred"].idxmin()) in best_two)
        if g["cams_t"].notna().all():
            hits["cams"].append(int(g["cams_t"].idxmin()) in best_two)
        hits["always_12"].append(12 in best_two)
        hits["always_13"].append(13 in best_two)
        assembly = g.loc[TARGET_HOURS[0], "target"]
        if assembly > 0:
            cuts.append((assembly - g.loc[int(g["pred"].idxmin()), "target"]) / assembly)
    result["station_days"] = len(hits["model"])
    result["hit_rate_top2"] = {k: round(float(np.mean(v)), 3) if v else None for k, v in hits.items()}
    result["realised_cut_vs_assembly"] = {
        "median": round(float(np.median(cuts)), 3) if cuts else None,
        "mean": round(float(np.mean(cuts)), 3) if cuts else None,
    }
    by_lead = ok.assign(err_model=(ok["pred"] - ok["target"]).abs(), err_persist=(ok["v_last"] - ok["target"]).abs())
    result["mae_by_lead"] = (
        by_lead.groupby("lead_h")[["err_model", "err_persist"]].mean().round(1).reset_index().to_dict("records")
    )
    return result
```

### `src/clearhour/meteo.py` (Verbatim)

```python
"""City-point weather and CAMS PM2.5 from Open-Meteo (no key; credit CAMS and Open-Meteo in the README).

One central-Delhi point is enough: CAMS is a ~45 km grid and the mixing-layer signal is city-wide.
Station-to-station differences come from each station's own recent readings.
"""

from __future__ import annotations

import pandas as pd
import requests

CITY = (28.6139, 77.2090)
WEATHER_ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"  # stitched past forecasts
WEATHER_LIVE = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"
WEATHER_VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "boundary_layer_height"]


def _hourly(url: str, params: dict) -> pd.DataFrame:
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    h = r.json()["hourly"]
    df = pd.DataFrame(h)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")


def fetch(start_date: str, end_date: str, *, live: bool = False) -> pd.DataFrame:
    """Hourly met for [start_date, end_date] (inclusive, YYYY-MM-DD), indexed by IST hour."""
    base = {
        "latitude": CITY[0],
        "longitude": CITY[1],
        "timezone": "GMT",
        "start_date": start_date,
        "end_date": end_date,
    }
    weather = _hourly(WEATHER_LIVE if live else WEATHER_ARCHIVE, {**base, "hourly": ",".join(WEATHER_VARS)})
    aq = _hourly(AIR_QUALITY, {**base, "hourly": "pm2_5", "domains": "cams_global"})
    met = weather.join(aq, how="outer")
    met.index = met.index.tz_convert("Asia/Kolkata")
    for col in [*WEATHER_VARS, "pm2_5"]:
        if col not in met:
            met[col] = float("nan")
    return met.astype(float)
```

### `src/clearhour/decide.py` (Verbatim)

```python
"""The decision rule and the four template variables of the WhatsApp alert (English and Hindi)."""

from __future__ import annotations

from datetime import date

from clearhour.constants import ASSEMBLY_HOUR, TARGET_HOURS

FINE_MAX = 90  # every school hour at or below: air is fine
SEVERE_MIN = 250  # every school hour above: no safe window
ASSEMBLY_INDOOR_MIN = 120  # assembly hour above: hold assembly indoors

EN_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EN_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
HI_DOW = ["सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि", "रवि"]
HI_MON = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]

TEXT = {
    "en": {
        "fine": "Air is fine for outdoor activity today.",
        "no_window": "No safe window today. Keep assembly, PE and recess indoors.",
        "assembly_in": "Hold assembly indoors.",
        "assembly_ok": "Assembly can stay outdoors.",
        "any_time": "any time",
        "none_today": "none today",
        "replay": " (replay)",
    },
    "hi": {
        "fine": "आज बाहरी गतिविधियों के लिए हवा ठीक है।",
        "no_window": "आज कोई सुरक्षित समय नहीं है। प्रार्थना सभा, पीटी और खेल अंदर ही कराएँ।",
        "assembly_in": "प्रार्थना सभा अंदर करें।",
        "assembly_ok": "प्रार्थना सभा बाहर हो सकती है।",
        "any_time": "किसी भी समय",
        "none_today": "आज कोई नहीं",
        "replay": " (रीप्ले)",
    },
}


def decide(hourly: dict[int, float]) -> dict:
    """hourly: predicted PM2.5 per school hour (08..13). Returns the kind of day and the Clear Hour."""
    vals = {h: float(hourly[h]) for h in TARGET_HOURS}
    if all(v <= FINE_MAX for v in vals.values()):
        return {"kind": "fine", "clear_hour": None, "assembly_indoors": False}
    if all(v > SEVERE_MIN for v in vals.values()):
        return {"kind": "no_window", "clear_hour": None, "assembly_indoors": True}
    clear = min(vals, key=vals.get)
    return {"kind": "clear_hour", "clear_hour": clear, "assembly_indoors": vals[ASSEMBLY_HOUR] > ASSEMBLY_INDOOR_MIN}


def _clock(h: int) -> tuple[int, str]:
    return (h % 12) or 12, "AM" if h % 24 < 12 else "PM"


def slot_label(hour: int, lang: str) -> str:
    """'1:00–2:00 PM' / '11:00 AM–12:00 PM' in English; 'दोपहर 1:00–2:00' in Hindi."""
    (a, ma), (b, mb) = _clock(hour), _clock(hour + 1)
    if lang == "hi":
        period = "सुबह" if hour < 12 else "दोपहर" if hour < 16 else "शाम"
        return f"{period} {a}:00–{b}:00"
    return f"{a}:00–{b}:00 {ma}" if ma == mb else f"{a}:00 {ma}–{b}:00 {mb}"


def day_label(day: date, lang: str) -> str:
    if lang == "hi":
        return f"{HI_DOW[day.weekday()]} {day.day} {HI_MON[day.month - 1]}"
    return f"{EN_DOW[day.weekday()]} {day.day} {EN_MON[day.month - 1]}"


def message_params(decision: dict, school_name: str, day: date, lang: str = "en", replay: bool = False) -> list[str]:
    """The template's {{1}}..{{4}}: school, day, advice sentence, Clear Hour."""
    t = TEXT[lang]
    when = day_label(day, lang) + (t["replay"] if replay else "")
    if decision["kind"] == "fine":
        advice, slot = t["fine"], t["any_time"]
    elif decision["kind"] == "no_window":
        advice, slot = t["no_window"], t["none_today"]
    else:
        advice = t["assembly_in"] if decision["assembly_indoors"] else t["assembly_ok"]
        slot = slot_label(decision["clear_hour"], lang)
    return [school_name, when, advice, slot]
```

### `src/clearhour/whatsapp.py` (Verbatim)

```python
"""WhatsApp payloads, sending (AWS End User Messaging Social, Meta Cloud API, or dry run) and inbound parsing."""

from __future__ import annotations

import json
import os
import urllib.request

import boto3

DEFAULT_META_API_VERSION = "v20.0"


def template_payload(to: str, params: list[str], *, name: str, lang: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}],
        },
    }


def text_payload(to: str, body: str) -> dict:
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}


def send(payload: dict) -> str:
    """Send one message; returns the provider's message id. WA_MODE picks the route."""
    mode = os.environ.get("WA_MODE", "dry_run")
    version = os.environ.get("META_API_VERSION", DEFAULT_META_API_VERSION)
    if mode == "eum":
        resp = boto3.client("socialmessaging").send_whatsapp_message(
            originationPhoneNumberId=os.environ["WA_PHONE_NUMBER_ID"],
            message=json.dumps(payload).encode(),
            metaApiVersion=version,
        )
        return resp["messageId"]
    if mode == "meta":  # Plan B: Meta's Cloud API directly, e.g. with Meta's test number
        req = urllib.request.Request(
            f"https://graph.facebook.com/{version}/{os.environ['META_PHONE_NUMBER_ID']}/messages",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {os.environ['META_ACCESS_TOKEN']}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)["messages"][0]["id"]
    if mode == "dry_run":
        print(json.dumps({"dry_run_payload": payload}, ensure_ascii=False))
        return "dry-run"
    raise ValueError(f"unknown WA_MODE {mode!r}")


def parse_sns(event: dict) -> list[dict]:
    """Text messages from End User Messaging Social's SNS events: sender, text, message id, timestamp.

    The SNS message's whatsAppWebhookEntry is Meta's webhook entry as a JSON string.
    """
    out = []
    for record in event.get("Records", []):
        body = json.loads(record["Sns"]["Message"])
        entry = json.loads(body["whatsAppWebhookEntry"])
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                if msg.get("type") == "text":
                    out.append(
                        {
                            "from": msg["from"],
                            "text": msg["text"]["body"].strip(),
                            "id": msg["id"],
                            "timestamp": msg.get("timestamp"),
                        }
                    )
    return out
```

### `src/clearhour/store.py` (Verbatim)

```python
"""DynamoDB access for the single ClearHour table (keys as in CLAUDE.md)."""

from __future__ import annotations

import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

_TABLE = None


def table():
    global _TABLE
    if _TABLE is None:
        _TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _TABLE


def _scan(**kwargs) -> list[dict]:
    items, start = [], None
    while True:
        resp = table().scan(**kwargs, **({"ExclusiveStartKey": start} if start else {}))
        items += resp["Items"]
        start = resp.get("LastEvaluatedKey")
        if not start:
            return items


def put_obs(location_id: int, hour_utc: str, pm25: float, n: int, ttl_days: int = 60) -> None:
    table().put_item(
        Item={
            "pk": f"STATION#{location_id}",
            "sk": f"OBS#{hour_utc}",
            "pm25": Decimal(str(round(pm25, 2))),
            "n": n,
            "ttl": int(time.time()) + ttl_days * 86400,
        }
    )


def get_obs(location_id: int, start_utc: str, end_utc: str) -> list[dict]:
    """Hourly readings with hour_utc between the two ISO strings (inclusive)."""
    items, start = [], None
    cond = Key("pk").eq(f"STATION#{location_id}") & Key("sk").between(f"OBS#{start_utc}", f"OBS#{end_utc}")
    while True:
        resp = table().query(KeyConditionExpression=cond, **({"ExclusiveStartKey": start} if start else {}))
        items += resp["Items"]
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
    return [{"hour_utc": it["sk"][4:], "pm25": float(it["pm25"])} for it in items]


def profiles() -> list[dict]:
    """Subscribed schools: SCHOOL#<id> / PROFILE with name, phone, lang."""
    return _scan(FilterExpression=Attr("sk").eq("PROFILE"))


def profile_by_phone(phone: str) -> dict | None:
    hits = _scan(FilterExpression=Attr("sk").eq("PROFILE") & Attr("phone").eq(phone))
    return hits[0] if hits else None


def alert_key(school_id: str, day: str) -> dict:
    return {"pk": f"SCHOOL#{school_id}", "sk": f"ALERT#{day}"}


def put_alert_if_new(school_id: str, day: str, attrs: dict, *, overwrite: bool = False) -> bool:
    """Creates the day's alert in status 'pending'. False if one already exists (unless overwrite)."""
    item = {**alert_key(school_id, day), **attrs, "status": "pending", "ttl": int(time.time()) + 90 * 86400}
    try:
        if overwrite:
            table().put_item(Item=item)
        else:
            table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def get_alert(school_id: str, day: str) -> dict | None:
    return table().get_item(Key=alert_key(school_id, day)).get("Item")


def move_alert(school_id: str, day: str, from_status: str, to_status: str, **attrs) -> bool:
    """Atomically moves an alert between statuses; False if it wasn't in from_status."""
    names = {"#s": "status", **{f"#{k}": k for k in attrs}}
    values = {":from": from_status, ":to": to_status, **{f":{k}": v for k, v in attrs.items()}}
    sets = ", ".join(["#s = :to", *[f"#{k} = :{k}" for k in attrs]])
    try:
        table().update_item(
            Key=alert_key(school_id, day),
            UpdateExpression=f"SET {sets}",
            ConditionExpression="#s = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def mark_acted(school_id: str, day: str, at: str) -> bool:
    try:
        table().update_item(
            Key=alert_key(school_id, day),
            UpdateExpression="SET acted = :t, acted_at = :at",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":t": True, ":at": at},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
```

### `src/clearhour/handlers/ingest.py` (Verbatim)

```python
"""Ingest Lambda (hourly): latest PM2.5 for each monitor from the OpenAQ API into DynamoDB, one item per IST hour.

Readings are binned by the IST clock hour their period starts in, so the API's explicit periods need no
start/end convention. The 3-hour lookback rewrites recent hours as late readings arrive.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from importlib import resources
from zoneinfo import ZoneInfo

from clearhour import store

API = "https://api.openaq.org/v3"
IST = ZoneInfo("Asia/Kolkata")


def stations() -> list[dict]:
    return json.loads(resources.files("clearhour").joinpath("stations.json").read_text())


def _get(path: str, params: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}?{urllib.parse.urlencode(params)}",
        headers={"X-API-Key": os.environ["OPENAQ_API_KEY"], "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def hourly_means(results: list[dict]) -> dict[str, tuple[float, int]]:
    """{hour start in UTC ISO: (mean, count)} from raw measurement results."""
    buckets: dict[str, list[float]] = {}
    for m in results:
        v = m.get("value")
        if v is None or v < 0 or v > 1000:
            continue
        start = datetime.fromisoformat(m["period"]["datetimeFrom"]["utc"].replace("Z", "+00:00"))
        hour_ist = start.astimezone(IST).replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour_ist.astimezone(UTC).isoformat(), []).append(float(v))
    return {k: (sum(v) / len(v), len(v)) for k, v in buckets.items()}


def handler(event, context):
    now = datetime.now(UTC)
    since = now - timedelta(hours=int(os.environ.get("LOOKBACK_HOURS", "3")))
    written, failed = 0, []
    for s in stations():
        try:
            res = _get(
                f"/sensors/{s['pm25_sensor_id']}/measurements",
                {"datetime_from": since.isoformat(), "datetime_to": now.isoformat(), "limit": 1000},
            )["results"]
            for hour_utc, (mean, n) in hourly_means(res).items():
                store.put_obs(s["location_id"], hour_utc, mean, n)
                written += 1
        except Exception as e:  # one bad station must not stop the rest
            failed.append({"location_id": s["location_id"], "error": str(e)[:200]})
        time.sleep(float(os.environ.get("PAUSE_S", "0.5")))  # stay well inside OpenAQ's rate limit
    print(json.dumps({"written": written, "failed": len(failed), "examples": failed[:5]}))
    return {"written": written, "failed": len(failed)}
```

### `src/clearhour/handlers/forecast.py` (Verbatim)

```python
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
    rows = features.build_rows(hourly, met, [day])
    if rows.empty:
        raise RuntimeError(f"no station had a reading within {features.MAX_STALENESS_H} h of the 04:00 IST bin")
    rows["location_id"] = pd.Categorical(rows["location_id"], categories=meta["stations"])
    rows["pred"] = np.expm1(booster.predict(rows[features.FEATURES]))
    preds: dict[str, dict[str, float]] = {}
    for r in rows.itertuples():
        preds.setdefault(str(r.location_id), {})[str(r.target_hour)] = round(float(r.pred), 1)
    body = {
        "day": str(day.date()),
        "source": source,
        "generated_at": pd.Timestamp.now(tz=IST).isoformat(timespec="seconds"),
        "median_lead_h": float(rows["lead_h"].median()),
        "stations": preds,
    }
    key = f"runs/{day.date()}/{source}/forecast.json"
    _s3.put_object(
        Bucket=os.environ["DATA_BUCKET"], Key=key, Body=json.dumps(body).encode(), ContentType="application/json"
    )
    print(
        json.dumps(
            {"day": body["day"], "source": source, "stations": len(preds), "median_lead_h": body["median_lead_h"]}
        )
    )
    return {
        "day": body["day"],
        "source": source,
        "forecast_key": key,
        "stations": len(preds),
        "resend": bool(event.get("resend")),
    }
```

### `src/clearhour/handlers/decide.py` (Verbatim)

```python
"""Decide Lambda: station forecasts -> every school's decision (S3, for the dashboard) and today's alerts.

Alerts go only to subscribed schools (DynamoDB PROFILE items). Creating ALERT#<day> is conditional, so a
re-run never queues a second message; pass "resend": true to queue again (replays for the video).
"""

from __future__ import annotations

import json
import os
from datetime import date
from importlib import resources

import boto3

from clearhour import decide as rules
from clearhour import store
from clearhour.constants import TARGET_HOURS

_s3 = boto3.client("s3")


def schools() -> list[dict]:
    """[{id, name, lat, lon, near: [[location_id, weight], ...]}], built by scripts/build_reference.py."""
    return json.loads(resources.files("clearhour").joinpath("schools_index.json").read_text())


def school_hourly(school: dict, preds: dict[str, dict[str, float]]) -> dict[int, float] | None:
    """Inverse-distance blend of the nearby stations' forecasts; None if none of them reported."""
    out = {}
    for h in TARGET_HOURS:
        num = den = 0.0
        for station_id, weight in school["near"]:
            v = preds.get(str(station_id), {}).get(str(h))
            if v is not None:
                num += weight * v
                den += weight
        if den == 0:
            return None
        out[h] = num / den
    return out


def handler(event, context):
    bucket = os.environ["DATA_BUCKET"]
    fc = json.loads(_s3.get_object(Bucket=bucket, Key=event["forecast_key"])["Body"].read())
    day, replay = date.fromisoformat(fc["day"]), fc["source"] != "live"

    decisions = {}
    for sch in schools():
        hourly = school_hourly(sch, fc["stations"])
        if hourly:
            decisions[sch["id"]] = {
                **rules.decide(hourly),
                "name": sch["name"],
                "lat": sch["lat"],
                "lon": sch["lon"],
                "hourly": {str(h): round(v, 1) for h, v in hourly.items()},
            }
    decisions_key = event["forecast_key"].replace("forecast.json", "decisions.json")
    _s3.put_object(
        Bucket=bucket, Key=decisions_key, Body=json.dumps(decisions).encode(), ContentType="application/json"
    )

    alert_day = fc["day"] + ("-replay" if replay else "")
    alerts = []
    for p in store.profiles():
        school_id = p["pk"].split("#", 1)[1]
        d = decisions.get(school_id)
        if not d:
            continue
        attrs = {
            "params": rules.message_params(d, p["name"], day, p.get("lang", "en"), replay=replay),
            "kind": d["kind"],
            "clear_hour": d["clear_hour"],
            "assembly_indoors": d["assembly_indoors"],
        }
        created = store.put_alert_if_new(school_id, alert_day, attrs, overwrite=bool(event.get("resend")))
        existing = None if created else store.get_alert(school_id, alert_day)
        if created or (existing and existing.get("status") == "pending"):
            alerts.append({"school_id": school_id, "day": alert_day})
    print(json.dumps({"day": fc["day"], "schools": len(decisions), "alerts": len(alerts)}))
    return {"day": fc["day"], "decisions_key": decisions_key, "schools": len(decisions), "alerts": alerts}
```

### `src/clearhour/handlers/send.py` (Verbatim)

```python
"""Send Lambda (one school per call, from the Step Functions Map): pending alert -> WhatsApp template.

The alert moves pending -> sending -> sent with conditional writes, so a retried or duplicated call never
sends twice. A failed send goes back to pending and re-raises, so Step Functions can retry it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from clearhour import store, whatsapp


def handler(event, context):
    school_id, day = event["school_id"], event["day"]
    alert = store.get_alert(school_id, day)
    if not alert or alert.get("status") != "pending":
        return {"school_id": school_id, "status": "skipped", "reason": alert.get("status") if alert else "missing"}
    if not store.move_alert(school_id, day, "pending", "sending"):
        return {"school_id": school_id, "status": "skipped", "reason": "claimed by another run"}
    profile = store.table().get_item(Key={"pk": f"SCHOOL#{school_id}", "sk": "PROFILE"})["Item"]
    lang = profile.get("lang", "en")
    payload = whatsapp.template_payload(
        profile["phone"],
        list(alert["params"]),
        name=os.environ.get("WA_TEMPLATE_NAME", "clearhour_daily_alert"),
        lang=os.environ.get(f"WA_TEMPLATE_LANG_{lang.upper()}", lang),
    )
    try:
        message_id = whatsapp.send(payload)
    except Exception as e:
        store.move_alert(school_id, day, "sending", "pending", last_error=str(e)[:500])
        raise
    store.move_alert(
        school_id,
        day,
        "sending",
        "sent",
        message_id=message_id,
        sent_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return {"school_id": school_id, "status": "sent", "message_id": message_id}
```

### `src/clearhour/handlers/inbound.py` (Verbatim)

```python
"""Inbound Lambda (SNS from End User Messaging Social): a principal's reply.

"1" (or "१", "done") marks today's alert as acted on; anything else gets a short help reply. Replies are
free-form text, allowed because the principal has just messaged us (WhatsApp's 24-hour window).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from clearhour import store, whatsapp

IST = ZoneInfo("Asia/Kolkata")
YES = {"1", "1.", "१", "done", "ok 1", "हो गया"}

REPLIES = {
    "en": {
        "thanks": "Thanks, noted for {name} today.",
        "no_alert": "Thanks! There is no ClearHour alert for today yet.",
        "help": (
            "ClearHour sends one air alert each school morning by 6:30. Reply 1 once you have moved outdoor activity."
        ),
    },
    "hi": {
        "thanks": "धन्यवाद, आज {name} के लिए दर्ज कर लिया गया।",
        "no_alert": "धन्यवाद! आज के लिए अभी कोई ClearHour सूचना नहीं है।",
        "help": "ClearHour हर स्कूल सुबह 6:30 तक हवा की एक सूचना भेजता है। गतिविधियाँ बदलने के बाद 1 लिखकर भेजें।",
    },
}
UNKNOWN = "This number isn't registered with ClearHour yet."


def handler(event, context):
    handled = 0
    for msg in whatsapp.parse_sns(event):
        profile = store.profile_by_phone(msg["from"])
        if not profile:
            whatsapp.send(whatsapp.text_payload(msg["from"], UNKNOWN))
            continue
        school_id = profile["pk"].split("#", 1)[1]
        text = REPLIES.get(profile.get("lang", "en"), REPLIES["en"])
        if msg["text"].lower() in YES:
            today = datetime.now(IST).date().isoformat()
            acted = store.mark_acted(school_id, today, datetime.now(UTC).isoformat(timespec="seconds"))
            body = text["thanks"].format(name=profile["name"]) if acted else text["no_alert"]
        else:
            body = text["help"]
        whatsapp.send(whatsapp.text_payload(msg["from"], body))
        handled += 1
    return {"handled": handled}
```

### `tests/synthetic.py` (Verbatim)

```python
"""Synthetic Delhi-like data shared by the tests: morning peak, afternoon dip, 5% missing hours."""

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"


def make_synthetic(n_stations: int = 6, start: str = "2025-10-01", end: str = "2026-01-31", seed: int = 3):
    """Hourly station PM2.5 and a city-point met frame with a realistic morning peak and afternoon dip."""
    rng = np.random.default_rng(seed)
    hours = pd.date_range(start, end, freq="h", tz=IST, inclusive="left")
    h = hours.hour.to_numpy()
    blh = 150 + 900 * np.clip(np.sin((h - 7) / 11 * np.pi), 0, None)  # low at night, peak mid-afternoon
    day_level = np.repeat(rng.normal(0, 0.25, len(hours) // 24 + 1).cumsum() * 0.2, 24)[: len(hours)]
    city = np.exp(np.log(180) + day_level) * (1.4 - 0.9 * (blh - 150) / 900)
    met = pd.DataFrame(
        {
            "pm2_5": city * rng.normal(1.0, 0.15, len(hours)),
            "temperature_2m": 18 + 8 * np.sin((h - 9) / 24 * 2 * np.pi),
            "relative_humidity_2m": 70 - 20 * np.sin((h - 9) / 24 * 2 * np.pi),
            "wind_speed_10m": 4 + rng.normal(0, 1, len(hours)).clip(-3, 3),
            "boundary_layer_height": blh,
        },
        index=hours,
    )
    frames = []
    for s in range(n_stations):
        noise = rng.normal(1.0, 0.08, len(hours))
        v = city * (0.8 + 0.1 * s) * noise
        keep = rng.random(len(hours)) > 0.05  # 5% missing hours
        frames.append(pd.DataFrame({"location_id": 100 + s, "hour_ist": hours[keep], "pm25": v[keep]}))
    return pd.concat(frames, ignore_index=True), met
```

### `tests/test_model.py` (Verbatim)

```python
import pandas as pd
from synthetic import make_synthetic

from clearhour.features import FEATURES, MET_COLUMNS, build_rows
from clearhour.model import evaluate, walk_forward, with_station_category

IST = "Asia/Kolkata"


def test_rows_have_every_feature_and_sane_leads():
    hourly, met = make_synthetic(n_stations=2, end="2025-11-01")
    days = pd.date_range("2025-10-05", "2025-10-30", freq="D", tz=IST)
    rows = build_rows(hourly, met, days)
    assert list(rows.columns) == ["day", "target", *FEATURES]
    assert set(rows["target_hour"]) == set(range(8, 14))
    assert rows["lead_h"].between(4, 4 + 9 + 6).all()  # 08:00 is 4 h after 04:00; staleness adds up to 6 h
    assert set(MET_COLUMNS) <= set(met.columns)
    # yesterday-same-hour feature really is yesterday's reading at that hour
    one = rows[(rows["location_id"] == 100) & (rows["day"] == pd.Timestamp("2025-10-10", tz=IST))]
    yday = hourly[(hourly["location_id"] == 100) & (hourly["hour_ist"] == pd.Timestamp("2025-10-09 10:00", tz=IST))]
    if not yday.empty:
        assert abs(one.loc[one["target_hour"] == 10, "v_yday_t"].iloc[0] - yday["pm25"].iloc[0]) < 1e-9


def test_walk_forward_beats_persistence_and_reports_decisions():
    hourly, met = make_synthetic()
    days = pd.date_range("2025-10-03", "2026-01-30", freq="D", tz=IST)
    stations = sorted(hourly["location_id"].unique())
    rows = with_station_category(build_rows(hourly, met, days), stations)
    weeks = list(pd.date_range("2025-11-10", "2026-01-26", freq="7D", tz=IST))
    pred = walk_forward(rows, weeks, num_rounds=80)
    assert not pred.empty and pred["pred"].notna().all()
    res = evaluate(pred)
    assert res["mae"]["model"] < res["mae"]["persistence"]
    assert res["station_days"] > 100
    assert 0 <= res["hit_rate_top2"]["model"] <= 1
    assert res["realised_cut_vs_assembly"]["median"] > 0
```

### `tests/test_rules_and_messages.py` (Verbatim)

```python
import json
from datetime import date

from clearhour.decide import decide, message_params, slot_label
from clearhour.handlers.ingest import hourly_means
from clearhour.whatsapp import parse_sns, template_payload


def _hours(*vals):
    return dict(zip(range(8, 14), vals, strict=True))


def test_decision_rule_three_kinds():
    assert decide(_hours(60, 70, 80, 90, 85, 75))["kind"] == "fine"
    assert decide(_hours(400, 380, 350, 320, 300, 260))["kind"] == "no_window"
    d = decide(_hours(300, 260, 220, 180, 150, 160))
    assert d == {"kind": "clear_hour", "clear_hour": 12, "assembly_indoors": True}
    assert decide(_hours(110, 100, 95, 92, 91, 140))["assembly_indoors"] is False


def test_labels_and_params_in_both_languages():
    assert slot_label(13, "en") == "1:00–2:00 PM"
    assert slot_label(11, "en") == "11:00 AM–12:00 PM"
    assert slot_label(13, "hi") == "दोपहर 1:00–2:00"
    d = {"kind": "clear_hour", "clear_hour": 13, "assembly_indoors": True}
    en = message_params(d, "Sarvodaya Vidyalaya", date(2025, 11, 13), "en")
    assert en == ["Sarvodaya Vidyalaya", "Thu 13 Nov", "Hold assembly indoors.", "1:00–2:00 PM"]
    hi = message_params(d, "सर्वोदय विद्यालय", date(2025, 11, 13), "hi", replay=True)
    assert hi == ["सर्वोदय विद्यालय", "गुरु 13 नवंबर (रीप्ले)", "प्रार्थना सभा अंदर करें।", "दोपहर 1:00–2:00"]
    for p in en + hi:  # WhatsApp rejects template parameters with newlines or tabs
        assert "\n" not in p and "\t" not in p


def test_template_payload_shape():
    p = template_payload("919999999999", ["a", "b", "c", "d"], name="clearhour_daily_alert", lang="en")
    assert p["type"] == "template" and p["template"]["language"] == {"code": "en"}
    assert [x["text"] for x in p["template"]["components"][0]["parameters"]] == ["a", "b", "c", "d"]


def test_parse_sns_reads_text_messages_and_ignores_statuses():
    entry = {
        "id": "1",
        "changes": [
            {
                "value": {
                    "messages": [
                        {
                            "from": "919999999999",
                            "id": "wamid.X",
                            "timestamp": "1",
                            "type": "text",
                            "text": {"body": " 1 "},
                        }
                    ]
                }
            },
            {"value": {"statuses": [{"id": "wamid.Y", "status": "sent"}]}},
        ],
    }
    event = {"Records": [{"Sns": {"Message": json.dumps({"context": {}, "whatsAppWebhookEntry": json.dumps(entry)})}}]}
    assert parse_sns(event) == [{"from": "919999999999", "text": "1", "id": "wamid.X", "timestamp": "1"}]


def test_ingest_bins_fifteen_minute_periods_by_ist_clock_hour():
    def m(start_utc, v):
        return {"value": v, "period": {"datetimeFrom": {"utc": start_utc}}}

    # 08:00-09:00 IST is 02:30-03:30 UTC; a 08:45 IST start (03:15 UTC) belongs to the 08:00 hour
    res = [
        m("2025-11-13T02:30:00Z", 100),
        m("2025-11-13T02:45:00Z", 110),
        m("2025-11-13T03:15:00Z", 130),
        m("2025-11-13T03:30:00Z", 50),
        m("2025-11-13T03:45:00Z", -999),
    ]
    out = hourly_means(res)
    assert out["2025-11-13T02:30:00+00:00"] == (340 / 3, 3)
    assert out["2025-11-13T03:30:00+00:00"] == (50.0, 1)  # -999 (missing) dropped
```

### `tests/test_pipeline.py` (Verbatim)

```python
"""End to end on mocked AWS (moto): forecast -> decide -> send -> reply, with WhatsApp in dry-run mode."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import pandas as pd
import pytest
from moto import mock_aws
from synthetic import make_synthetic

from clearhour import features, store
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
        yield


def _seed_model_and_obs(monkeypatch, day: pd.Timestamp):
    from clearhour.handlers import forecast

    hourly, met = make_synthetic(n_stations=3, start="2025-10-01", end="2025-11-20")
    stations = [int(s) for s in sorted(hourly["location_id"].unique())]  # plain ints: they go into JSON
    train_days = pd.date_range("2025-10-03", "2025-11-10", tz="Asia/Kolkata")
    rows = with_station_category(features.build_rows(hourly, met, train_days), stations)
    assert not rows.empty
    booster = fit(rows, num_rounds=30)
    s3 = boto3.client("s3")
    s3.put_object(Bucket=BUCKET, Key="models/clearhour-lgbm.txt", Body=booster.model_to_string().encode())
    s3.put_object(
        Bucket=BUCKET,
        Key="models/features.json",
        Body=json.dumps({"features": features.FEATURES, "stations": stations}).encode(),
    )
    recent = hourly[
        (hourly["hour_ist"] >= day - pd.Timedelta(days=2)) & (hourly["hour_ist"] < day + pd.Timedelta(hours=5))
    ]
    for r in recent.itertuples():
        store.put_obs(int(r.location_id), r.hour_ist.tz_convert("UTC").isoformat(), float(r.pm25), 4)
    monkeypatch.setattr(forecast, "_s3", s3)
    monkeypatch.setattr(forecast, "_MODEL", None)
    monkeypatch.setattr(forecast, "stations", lambda: STATIONS)
    monkeypatch.setattr(forecast.meteo, "fetch", lambda *a, **k: met)
    return forecast


def test_forecast_decide_send_reply(aws, monkeypatch):
    from clearhour.handlers import decide, inbound, send

    day = pd.Timestamp("2025-11-13", tz="Asia/Kolkata")
    forecast = _seed_model_and_obs(monkeypatch, day)
    out = forecast.handler({"source": "live", "as_of": "2025-11-13T05:30:00+05:30"}, None)
    assert out["stations"] == 3 and out["forecast_key"] == "runs/2025-11-13/live/forecast.json"

    store.table().put_item(
        Item={"pk": "SCHOOL#node/1", "sk": "PROFILE", "name": "सर्वोदय विद्यालय", "phone": "919999999999", "lang": "hi"}
    )
    monkeypatch.setattr(decide, "_s3", boto3.client("s3"))
    monkeypatch.setattr(decide, "schools", lambda: SCHOOLS)
    res = decide.handler(out, None)
    assert res["schools"] == 1  # the far school has no station forecast
    assert res["alerts"] == [{"school_id": "node/1", "day": "2025-11-13"}]
    alert = store.get_alert("node/1", "2025-11-13")
    assert alert["status"] == "pending" and alert["params"][1] == "गुरु 13 नवंबर"

    sent = send.handler(res["alerts"][0], None)
    assert sent == {"school_id": "node/1", "status": "sent", "message_id": "dry-run"}
    assert send.handler(res["alerts"][0], None)["status"] == "skipped"  # never twice
    assert decide.handler(out, None)["alerts"] == []  # a re-run doesn't queue it again

    today = datetime.now(IST).date().isoformat()
    store.put_alert_if_new("node/1", today, {"params": ["a", "b", "c", "d"]})
    entry = {
        "changes": [
            {"value": {"messages": [{"from": "919999999999", "id": "w", "type": "text", "text": {"body": "१"}}]}}
        ]
    }
    event = {"Records": [{"Sns": {"Message": json.dumps({"whatsAppWebhookEntry": json.dumps(entry)})}}]}
    assert inbound.handler(event, None) == {"handled": 1}
    assert store.get_alert("node/1", today)["acted"] is True
```

**Check:**
- `uv run pytest -q` → Phase 1's tests plus 8 new, all passing.
- `uv run ruff check` and `uv run ruff format --check` → clean.

Commit: `core: features, model, decision rule, WhatsApp, Lambda handlers`.

## P2-T2: Weather and CAMS history → `data/processed/meteo.parquet`

`scripts/fetch_meteo.py`:

- One `meteo.fetch(first_day, last_day, live=False)` call per month: October 2025 through February 2026, plus
  October 2026 up to yesterday.
- Concatenate the months, drop duplicate hours, and write the parquet with the IST hour as the index.
- Print the row count, the date range, and the share of missing values per column.

**Check:** `boundary_layer_height` should be missing in under 10% of hours. If it's mostly missing, stop and
show me, because it's the model's strongest physical signal.
Commit: `data: city-point weather and CAMS history`.

## P2-T3: Backtest → `outputs/backtest.json` and `outputs/backtest_by_lead.csv`

`scripts/backtest.py`:

- **Stations:** those still reporting with at least 60% coverage in 2025–26. Use the same set as `stations.json`
  in P2-T5, with ids as plain ints.
- **Rows:** `with_station_category(build_rows(hourly, met, days), stations)`, with days from 2025-10-03 to
  2026-01-31. `hourly` comes from `pm25_hourly.parquet` filtered to those stations; `met` comes from
  `meteo.parquet`.
- **Predictions:** `walk_forward(rows, mondays)`, for the Mondays from 2025-11-10 to 2026-01-26.
- **Output:** `res = evaluate(pred)`.
  - Write `outputs/backtest.json` with `res` plus the station count and the list of test weeks.
  - Write `res["mae_by_lead"]` to `outputs/backtest_by_lead.csv`.
- **Print:**
  - MAE for the model, persistence, CAMS and yesterday-same-hour
  - the four hit rates
  - the median realised cut

**Check:** stop and show me if either of these holds:
- the model's MAE is not below yesterday-same-hour's
- the model's hit rate is less than 0.02 above `always_13`'s

Either one means the forecast isn't earning its place, and we reframe before building on it.

Amended 8 Oct, 03:10: in `scripts/backtest.py` (not `model.py`), compute from `pred` and add to
`backtest.json` and the printout: the model's top-2 hit rate on the station-days where `always_13` misses, and
the share of station-days where the model picks 13:00. The stop rule is unchanged; if it triggers, show these
numbers alongside it.

Commit: `analysis: walk-forward backtest`.

## P2-T4: Final model → `models/clearhour-lgbm.txt` and `models/features.json`

`scripts/train_final.py`:

- Build rows for every day from 2025-10-03 to the last full day in the parquet, including Feb 2026 and Oct 2026.
- Fit, then save `booster.model_to_string()` to `models/clearhour-lgbm.txt`.
- Save `models/features.json` as
  `{"features": FEATURES, "stations": [plain ints], "trained_through": "<date>", "num_rounds": NUM_ROUNDS}`.
- Gitignore `models/*.txt` and commit `features.json`.

**Check:** reload the model from the file, predict one day, and print the six school-hour predictions for two
stations.
Commit: `model: train final model`.

## P2-T5: Reference files the Lambdas carry → `src/clearhour/stations.json` and `src/clearhour/schools_index.json`

`scripts/build_reference.py`:

- `stations.json`: the P2-T3 station set, with `location_id`, `pm25_sensor_id`, `name`, `lat`, `lon` and `provider`.
- `schools_index.json`: every school in `data/schools.csv`.
  - Fields: `id` (the osm_id), `name` (or `School <osm_id>` when blank), `lat`, `lon`, and `near`.
  - `near` holds up to three stations within 10 km, weighted by 1/d² and normalised to sum to 1. If no station
    is within 10 km, it holds the nearest one with weight 1.
  - Round coordinates to 5 decimals and weights to 3. Keep the file under 2 MB.
- Print the counts and the file sizes.

**Check:** the five pilot schools appear, each with its station or stations. Commit both JSON files.
Commit: `data: Lambda reference files`.

## P2-T6: Deploy the pipeline (ask me before deploying)

1. Replace `template.yaml` with the Verbatim below, and add `functions/forecast/Dockerfile`,
   `functions/forecast/requirements.txt` and `statemachine/daily.asl.json`. Delete `functions/heartbeat/`; the
   stack drops that Lambda.
2. Write `scripts/deploy.sh` to do the following:
   - Load `.env` with `set -a; source .env; set +a`, then run `sam build`.
   - Run `sam deploy --resolve-image-repos --profile clearhour --parameter-overrides ...`, built from `.env`:
     - always: `OpenAqApiKey=$OPENAQ_API_KEY` and `WaMode=${WA_MODE:-dry_run}`
     - only when the variable is non-empty: `WaPhoneNumberId`, `WhatsAppEventsTopicArn`, `WaTemplateLangEn`,
       `WaTemplateLangHi`, `MetaPhoneNumberId`, `MetaAccessToken`
   - After deploying, take the bucket name from the stack outputs and upload:
     - `models/clearhour-lgbm.txt` and `models/features.json` to `s3://<DataBucketName>/models/`
     - `data/processed/pm25_hourly.parquet` to `s3://<DataBucketName>/archive/pm25_hourly.parquet`
3. Run it. Afterwards, check that `git diff samconfig.toml` doesn't contain the API key.

If `sam build` can't find python3.13, put it on PATH (for example with `uv python install 3.13`) or build with
`sam build --use-container`.

### `template.yaml` (Verbatim)

```yaml
AWSTemplateFormatVersion: "2010-09-09"
Transform: AWS::Serverless-2016-10-31
Description: ClearHour - hourly ingest, 05:30 IST forecast-decide-send run, WhatsApp replies

Parameters:
  OpenAqApiKey:
    Type: String
    NoEcho: true
  WaMode:
    Type: String
    Default: dry_run
    AllowedValues: [dry_run, eum, meta]
  WaPhoneNumberId:
    Type: String
    Default: ""
    Description: End User Messaging Social phone number id (phone-number-id-...)
  WaTemplateName:
    Type: String
    Default: clearhour_daily_alert
  WaTemplateLangEn:
    Type: String
    Default: en
  WaTemplateLangHi:
    Type: String
    Default: hi
  MetaApiVersion:
    Type: String
    Default: v20.0
  MetaPhoneNumberId:
    Type: String
    Default: ""
    Description: Plan B only - Meta Cloud API phone number id
  MetaAccessToken:
    Type: String
    Default: ""
    NoEcho: true
    Description: Plan B only - Meta Cloud API access token
  WhatsAppEventsTopicArn:
    Type: String
    Default: ""
    Description: SNS topic that End User Messaging Social publishes incoming messages to

Conditions:
  HasEventsTopic: !Not [!Equals [!Ref WhatsAppEventsTopicArn, ""]]

Globals:
  Function:
    Architectures: [x86_64]
    MemorySize: 256
    Timeout: 60
    Environment:
      Variables:
        TABLE_NAME: !Ref ClearHourTable
        DATA_BUCKET: !Ref DataBucket

Resources:
  ClearHourTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      AttributeDefinitions:
        - AttributeName: pk
          AttributeType: S
        - AttributeName: sk
          AttributeType: S
      KeySchema:
        - AttributeName: pk
          KeyType: HASH
        - AttributeName: sk
          KeyType: RANGE
      TimeToLiveSpecification:
        AttributeName: ttl
        Enabled: true

  DataBucket:
    Type: AWS::S3::Bucket
    Properties:
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault:
              SSEAlgorithm: AES256

  IngestFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/
      Handler: clearhour.handlers.ingest.handler
      Runtime: python3.13
      Timeout: 180
      Environment:
        Variables:
          OPENAQ_API_KEY: !Ref OpenAqApiKey
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref ClearHourTable
      Events:
        Hourly:
          Type: ScheduleV2
          Properties:
            ScheduleExpression: cron(10 * * * ? *)

  ForecastFunction:
    Type: AWS::Serverless::Function
    Properties:
      PackageType: Image
      MemorySize: 2048
      Timeout: 180
      Policies:
        - DynamoDBReadPolicy:
            TableName: !Ref ClearHourTable
        - S3CrudPolicy:
            BucketName: !Ref DataBucket
    Metadata:
      Dockerfile: functions/forecast/Dockerfile
      DockerContext: .
      DockerTag: v1

  DecideFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/
      Handler: clearhour.handlers.decide.handler
      Runtime: python3.13
      MemorySize: 512
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref ClearHourTable
        - S3CrudPolicy:
            BucketName: !Ref DataBucket

  SendFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/
      Handler: clearhour.handlers.send.handler
      Runtime: python3.13
      Environment:
        Variables:
          WA_MODE: !Ref WaMode
          WA_PHONE_NUMBER_ID: !Ref WaPhoneNumberId
          WA_TEMPLATE_NAME: !Ref WaTemplateName
          WA_TEMPLATE_LANG_EN: !Ref WaTemplateLangEn
          WA_TEMPLATE_LANG_HI: !Ref WaTemplateLangHi
          META_API_VERSION: !Ref MetaApiVersion
          META_PHONE_NUMBER_ID: !Ref MetaPhoneNumberId
          META_ACCESS_TOKEN: !Ref MetaAccessToken
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref ClearHourTable
        - Statement:
            - Effect: Allow
              Action: social-messaging:SendWhatsAppMessage
              Resource: "*"

  InboundFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/
      Handler: clearhour.handlers.inbound.handler
      Runtime: python3.13
      Environment:
        Variables:
          WA_MODE: !Ref WaMode
          WA_PHONE_NUMBER_ID: !Ref WaPhoneNumberId
          WA_TEMPLATE_NAME: !Ref WaTemplateName
          WA_TEMPLATE_LANG_EN: !Ref WaTemplateLangEn
          WA_TEMPLATE_LANG_HI: !Ref WaTemplateLangHi
          META_API_VERSION: !Ref MetaApiVersion
          META_PHONE_NUMBER_ID: !Ref MetaPhoneNumberId
          META_ACCESS_TOKEN: !Ref MetaAccessToken
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref ClearHourTable
        - Statement:
            - Effect: Allow
              Action: social-messaging:SendWhatsAppMessage
              Resource: "*"

  InboundSubscription:
    Type: AWS::SNS::Subscription
    Condition: HasEventsTopic
    Properties:
      Protocol: lambda
      TopicArn: !Ref WhatsAppEventsTopicArn
      Endpoint: !GetAtt InboundFunction.Arn

  InboundPermission:
    Type: AWS::Lambda::Permission
    Condition: HasEventsTopic
    Properties:
      Action: lambda:InvokeFunction
      FunctionName: !Ref InboundFunction
      Principal: sns.amazonaws.com
      SourceArn: !Ref WhatsAppEventsTopicArn

  DailyRun:
    Type: AWS::Serverless::StateMachine
    Properties:
      DefinitionUri: statemachine/daily.asl.json
      DefinitionSubstitutions:
        ForecastFunctionArn: !GetAtt ForecastFunction.Arn
        DecideFunctionArn: !GetAtt DecideFunction.Arn
        SendFunctionArn: !GetAtt SendFunction.Arn
      Policies:
        - LambdaInvokePolicy:
            FunctionName: !Ref ForecastFunction
        - LambdaInvokePolicy:
            FunctionName: !Ref DecideFunction
        - LambdaInvokePolicy:
            FunctionName: !Ref SendFunction
      Events:
        Morning:
          Type: ScheduleV2
          Properties:
            ScheduleExpression: cron(30 5 * * ? *)
            ScheduleExpressionTimezone: Asia/Kolkata
            Input: '{"source": "live"}'

Outputs:
  TableName:
    Value: !Ref ClearHourTable
  DataBucketName:
    Value: !Ref DataBucket
  DailyRunArn:
    Value: !Ref DailyRun
  ForecastFunctionName:
    Value: !Ref ForecastFunction
  IngestFunctionName:
    Value: !Ref IngestFunction
```

### `functions/forecast/Dockerfile` (Verbatim)

`lib_lightgbm.so` links against the system `libgomp.so.1`, so the install line is required.

```dockerfile
FROM public.ecr.aws/lambda/python:3.13

# LightGBM needs the OpenMP runtime; the Python 3.13 base image is Amazon Linux 2023 minimal (dnf = microdnf)
RUN dnf install -y libgomp && dnf clean all

COPY functions/forecast/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY src/clearhour ${LAMBDA_TASK_ROOT}/clearhour

CMD ["clearhour.handlers.forecast.handler"]
```

### `functions/forecast/requirements.txt` (Verbatim)

```text
lightgbm==4.7.0
numpy==2.5.3
pandas==3.0.6
pyarrow==25.0.1
requests==2.34.2
```

### `statemachine/daily.asl.json` (Verbatim)

```json
{
  "Comment": "ClearHour daily run: forecast every station, decide every school, send the subscribed schools' alerts",
  "StartAt": "Forecast",
  "States": {
    "Forecast": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": { "FunctionName": "${ForecastFunctionArn}", "Payload.$": "$" },
      "OutputPath": "$.Payload",
      "Retry": [
        {
          "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"],
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
      "Parameters": { "FunctionName": "${DecideFunctionArn}", "Payload.$": "$" },
      "OutputPath": "$.Payload",
      "Retry": [
        {
          "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"],
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
        "ProcessorConfig": { "Mode": "INLINE" },
        "StartAt": "Send",
        "States": {
          "Send": {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": { "FunctionName": "${SendFunctionArn}", "Payload.$": "$" },
            "OutputPath": "$.Payload",
            "Retry": [{ "ErrorEquals": ["States.ALL"], "IntervalSeconds": 10, "MaxAttempts": 2, "BackoffRate": 2 }],
            "Catch": [{ "ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "SendFailed" }],
            "End": true
          },
          "SendFailed": { "Type": "Pass", "End": true }
        }
      },
      "ResultPath": "$.sent",
      "End": true
    }
  }
}
```

**Check:**

- `sam remote invoke IngestFunction --stack-name clearhour --profile clearhour` → `written` > 0, with only a
  few failures.
- Measure live latency: compare each station's newest `OBS#` hour in DynamoDB with the current time, and print
  the median lag in hours. If the 04:00 IST bin would usually be missing at 05:30, tell me. `LATEST_OBS_HOUR`
  then moves earlier, and T3 and T4 re-run, which takes about a minute.

Commit: `infra: daily pipeline (ingest, forecast, decide, send, inbound)`.

## P2-T7: First run, then the real message

1. **Pilots.** Write `scripts/seed_pilots.py`:
   - It reads `data/pilot_schools.csv`, plus `PILOT_PHONES` and `PILOT_LANGS` from `.env`.
     - `PILOT_PHONES` is comma-separated, country code plus digits, in the CSV's order. Fewer numbers than
       schools is fine.
     - `PILOT_LANGS` is a matching list, e.g. `hi,en`.
   - It writes a `SCHOOL#<osm_id>` / `PROFILE` item per school with a number, holding `name`, `phone` and `lang`.
   - It never prints full phone numbers.

   For the demo, one pilot school with Shreyash's own number is enough.
2. **Dry run** (`WA_MODE=dry_run`). Start the state machine twice and read the Send function's logs:
   - `{"source": "archive", "as_of": "2025-11-13", "resend": true}` → the payload's date says replay, and the
     advice fits that morning.
   - `{"source": "live", "resend": true}`

   Start each run with
   `aws stepfunctions start-execution --state-machine-arn <DailyRunArn> --input '<json>' --profile clearhour`.
3. **Real message**, once WhatsApp is ready:
   1. In `.env`, set `WA_MODE=eum`, `WA_PHONE_NUMBER_ID` and `WA_EVENTS_TOPIC_ARN` (see Appendix A), then
      redeploy.
   2. Message the business number from the phone first. That's the opt-in, and it opens the 24-hour window for
      replies.
   3. Start a live run with `"resend": true`.
   4. When the alert arrives, reply 1. The ALERT item should show `acted: true`, and the thank-you reply should
      come back.

**Check:** screen-record the phone and the Step Functions execution graph turning green. Both go in the video.
Commit: `ops: seed pilots and first live run`.

---

## Appendix A: Finding the WhatsApp values

- **`WA_PHONE_NUMBER_ID`** (format `phone-number-id-…`):
  1. `aws socialmessaging list-linked-whatsapp-business-accounts --profile clearhour` gives the account id.
  2. `aws socialmessaging get-linked-whatsapp-business-account --id <that id> --profile clearhour` lists the
     account's phone numbers and their ids.
- **`WA_EVENTS_TOPIC_ARN`**: the SNS topic set as the account's message and event destination in Phase 1 (A3).
- **Template language codes:** the send uses `en` and `hi`. If WhatsApp Manager lists the template under a
  regional code such as `en_US`, put that code in `WA_TEMPLATE_LANG_EN` or `WA_TEMPLATE_LANG_HI`.
- **Plan B (Meta Cloud API):** set `WA_MODE=meta`, plus `META_PHONE_NUMBER_ID` and `META_ACCESS_TOKEN` from
  Meta's developer app (the test number). Sending works. Replies don't reach us in Plan B, since that would need
  a webhook endpoint. Record the "reply 1" moment only on Plan A.

## Appendix B: Report back to the architect

```
PHASE 2 REPORT
Meteo: <rows> hours, <first> to <last>; boundary_layer_height missing <x>%
Backtest (<n> stations, 12 weeks): MAE model <x> · persistence <x> · CAMS <x> · yesterday <x>
Hit rate top-2: model <x> · always_13 <x> · always_12 <x> · CAMS <x>; realised cut median <x>
Final model: trained through <date>, <n> stations
Reference: stations <n>, schools <n>, pilots matched <n>/5
Deploy: image build <ok?>; ingest written <n>, failed <n>; median live lag <h> h
Runs: replay <ok?> · live dry run <ok?> · real WhatsApp delivered <yes/no> · reply marked acted <yes/no>
Failures (verbatim): <...>
DECISIONS.md: <new lines>
```
