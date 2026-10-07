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
