"""P2-T5: reference files the Lambdas carry -> src/clearhour/stations.json and src/clearhour/schools_index.json."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
from config import model_stations
from schools import PILOTS, pairwise_km

PKG = Path("src/clearhour")
NEAR_KM = 10.0
MAX_NEAR = 3
MIN_KM = 0.1  # a school on a station's doorstep would otherwise get an infinite 1/d² weight


def near_stations(dist: np.ndarray, ids: np.ndarray) -> list[list]:
    """Up to three stations within 10 km weighted by 1/d², normalised; else the nearest one with weight 1."""
    order = np.argsort(dist)[:MAX_NEAR]
    within = [j for j in order if dist[j] <= NEAR_KM]
    if not within:
        return [[int(ids[order[0]]), 1.0]]
    w = 1 / np.maximum(dist[within], MIN_KM) ** 2
    return [[int(ids[j]), round(float(x), 3)] for j, x in zip(within, w / w.sum(), strict=True)]


def main() -> None:
    s = pd.read_csv("data/stations.csv")
    s = s[s["location_id"].isin(model_stations())].sort_values("location_id")
    stations = [
        {
            "location_id": int(r["location_id"]),
            "pm25_sensor_id": int(r["pm25_sensor_id"]),
            "name": r["name"],
            "lat": round(float(r["lat"]), 5),
            "lon": round(float(r["lon"]), 5),
            "provider": r["provider"],
        }
        for _, r in s.iterrows()
    ]
    schools = pd.read_csv("data/schools.csv", keep_default_na=False)
    d, ids = pairwise_km(schools, s), s["location_id"].to_numpy()
    index = [
        {
            "id": r["osm_id"],
            "name": r["name"].strip() or f"School {r['osm_id']}",
            "lat": round(float(r["lat"]), 5),
            "lon": round(float(r["lon"]), 5),
            "near": near_stations(d[i], ids),
        }
        for i, (_, r) in enumerate(schools.iterrows())
    ]
    (PKG / "stations.json").write_text(json.dumps(stations, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = ",\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in index)
    (PKG / "schools_index.json").write_text(f"[\n{lines}\n]\n", encoding="utf-8")

    sizes = {f: (PKG / f).stat().st_size for f in ("stations.json", "schools_index.json")}
    n_near = pd.Series([len(x["near"]) for x in index]).value_counts().sort_index().to_dict()
    print(f"stations.json: {len(stations)} stations, {sizes['stations.json']:,} bytes")
    print(f"schools_index.json: {len(index):,} schools, {sizes['schools_index.json']:,} bytes")
    print(f"stations per school: {n_near}")
    print(f"schools with no station within {NEAR_KM:.0f} km (nearest one, weight 1): {(d.min(axis=1) > NEAR_KM).sum()}")
    assert sizes["schools_index.json"] < 2_000_000, "schools_index.json must stay under 2 MB"

    names = s.set_index("location_id")["name"]
    by_id = {x["id"]: x for x in index}
    print("pilots:")
    for p in pd.read_csv(PILOTS).itertuples():
        near = " + ".join(f"{names[sid]} ({w})" for sid, w in by_id[p.osm_id]["near"])
        print(f"  {p.name} [{p.osm_id}]: {near}")


if __name__ == "__main__":
    main()
