"""Delhi schools from OpenStreetMap via the Overpass API."""

from __future__ import annotations

import requests

OVERPASS = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "clearhour-hackathon/0.1 (Environmental Hacks 2026)"}

BY_AREA = """
[out:json][timeout:180];
area["ISO3166-2"="IN-DL"]["admin_level"="4"]->.dl;
nwr["amenity"="school"](area.dl);
out center tags;
"""

# Fallback if the area lookup returns nothing: Delhi's bounding box as (south, west, north, east)
BY_BBOX = """
[out:json][timeout:180];
nwr["amenity"="school"](28.404,76.838,28.883,77.347);
out center tags;
"""


def _run(query: str) -> list[dict]:
    r = requests.post(OVERPASS, data={"data": query}, headers=HEADERS, timeout=200)
    r.raise_for_status()
    return r.json().get("elements", [])


def delhi_schools() -> tuple[list[dict], str]:
    """Returns the schools and which query produced them ("area" or "bbox")."""
    elements, used = _run(BY_AREA), "area"
    if not elements:
        elements, used = _run(BY_BBOX), "bbox"
    schools = []
    for el in elements:
        point = el if el["type"] == "node" else el.get("center")
        if not point:
            continue
        tags = el.get("tags", {})
        schools.append(
            {
                "osm_id": f"{el['type']}/{el['id']}",
                "name": tags.get("name") or tags.get("name:en") or "",
                "lat": point["lat"],
                "lon": point["lon"],
            }
        )
    return schools, used
