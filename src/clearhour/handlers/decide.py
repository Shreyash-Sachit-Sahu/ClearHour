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


def school_latest(school: dict, latest: dict[str, float]) -> float | None:
    """The newest measured PM2.5 near the school, blended with the same weights; None if no station has one."""
    pairs = [(w, latest[str(sid)]) for sid, w in school["near"] if str(sid) in latest]
    total = sum(w for w, _ in pairs)
    return sum(w * v for w, v in pairs) / total if total else None


def handler(event, context):
    bucket = os.environ["DATA_BUCKET"]
    fc = json.loads(_s3.get_object(Bucket=bucket, Key=event["forecast_key"])["Body"].read())
    day, replay = date.fromisoformat(fc["day"]), fc["source"] != "live"

    decisions = {}
    for sch in schools():
        hourly = school_hourly(sch, fc["stations"])
        if hourly:
            now = school_latest(sch, fc.get("latest", {}))
            decisions[sch["id"]] = {
                **rules.decide(hourly, latest=now),
                "name": sch["name"],
                "lat": sch["lat"],
                "lon": sch["lon"],
                "latest": None if now is None else round(now, 1),
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
