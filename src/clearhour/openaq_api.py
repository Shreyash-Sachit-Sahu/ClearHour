"""OpenAQ v3 API: find Delhi's reference PM2.5 monitors."""

from __future__ import annotations

import os

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
                    "pm25_sensor_id": pm25[0]["id"],
                    "first_utc": (loc.get("datetimeFirst") or {}).get("utc"),
                    "last_utc": (loc.get("datetimeLast") or {}).get("utc"),
                }
            )
        if len(results) < 100:
            return rows
        page += 1
