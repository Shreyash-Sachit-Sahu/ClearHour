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
        "obs_through": fc.get("obs_through"),
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
