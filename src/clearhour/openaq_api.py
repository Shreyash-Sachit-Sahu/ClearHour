"""OpenAQ v3 API: find Delhi's reference PM2.5 monitors."""

from __future__ import annotations

import os
import time

import requests

API = "https://api.openaq.org/v3"
# Delhi NCT as min lon, min lat, max lon, max lat (OpenAQ accepts up to 4 decimals)
DELHI_BBOX = "76.8380,28.4040,77.3470,28.8830"


def get(path: str, params: dict) -> dict:
    r = requests.get(
        f"{API}{path}",
        params=params,
        headers={"X-API-Key": os.environ["OPENAQ_API_KEY"]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def newest_sensor(ids: list[int]) -> int:
    """The sensor that reported last. A location can keep a retired PM2.5 sensor, and the list order isn't fixed."""
    if len(ids) == 1:
        return ids[0]
    last = {}
    for i in ids:
        time.sleep(1.1)  # OpenAQ allows 60 requests a minute
        last[i] = (get(f"/sensors/{i}", {})["results"][0].get("datetimeLast") or {}).get("utc") or ""
    return max(ids, key=lambda i: last[i])


def delhi_pm25_monitors() -> list[dict]:
    """Reference monitors (monitor=true) inside Delhi that carry a PM2.5 sensor."""
    rows: list[dict] = []
    page = 1
    while True:
        results = get(
            "/locations",
            {"bbox": DELHI_BBOX, "monitor": "true", "limit": 100, "page": page},
        )["results"]
        for loc in results:
            pm25 = [s for s in loc.get("sensors", []) if s["parameter"]["name"] == "pm25"]
            if not pm25:
                continue
            rows.append(
                {
                    "location_id": loc["id"],
                    "name": loc["name"],
                    "lat": loc["coordinates"]["latitude"],
                    "lon": loc["coordinates"]["longitude"],
                    "provider": (loc.get("provider") or {}).get("name"),
                    "owner": (loc.get("owner") or {}).get("name"),
                    "pm25_sensor_id": newest_sensor([s["id"] for s in pm25]),
                    "first_utc": (loc.get("datetimeFirst") or {}).get("utc"),
                    "last_utc": (loc.get("datetimeLast") or {}).get("utc"),
                }
            )
        if len(results) < 100:
            return rows
        page += 1
