"""T7: Delhi schools from OpenStreetMap -> data/schools.csv, and five pilot schools -> data/pilot_schools.csv."""

import re
from pathlib import Path

import numpy as np
import pandas as pd
from config import model_stations

from clearhour.overpass import delhi_schools

SCHOOLS = Path("data/schools.csv")
PILOTS = Path("data/pilot_schools.csv")
MAX_KM = 2.0
PREFERRED = r"sarvodaya|govt|government|kendriya"
N_PILOTS = 5
# A name made only of these words (ignoring digits and single letters) doesn't say which school it is.
SCHOOL_WORDS = set(
    "government govt school boys girls co ed coed senior sr secondary sec middle primary high higher public model "
    "sarvodaya sarvodya kendriya vidyalaya kanya bal rajkiya pratibha vikas nigam mcd ndmc gbsss gsss ggsss sss skv "
    "sbv sector block no number".split()
    + ["coeducational", "educational"]  # long forms of co/ed/coed ("Govt Coeducational Senior Secondary School")
)
OUTSIDE_DELHI = r"Noida|Gurugram|Gurgaon|Ghaziabad|Faridabad|Bahadurgarh"  # by name: an IMD station sits in Gurugram
# How the alert names each pilot (the PROFILE name); the OSM name stays in the name column.
DISPLAY_NAMES = {
    "way/295816372": "Kendriya Vidyalaya, RK Puram Sector 2",
    "way/78786253": "Rani Chenamma Sarvodaya Kanya Vidyalaya",
    "node/11696928200": "Kendriya Vidyalaya Shahdara",
    "way/1291730896": "Sarvodaya Kanya Vidyalaya, Noor Nagar",
    "node/2446010483": "Swami Dayanand Govt. Sarvodaya Vidyalaya",
}


def is_named(name: str) -> bool:
    words = [w for w in re.findall(r"[^\W\d_]+", name.lower()) if len(w) > 1]
    return any(w not in SCHOOL_WORDS for w in words)


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km; broadcasts over numpy arrays."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def pairwise_km(a: pd.DataFrame, b: pd.DataFrame) -> np.ndarray:
    """Distances from every row of a (rows) to every row of b (columns)."""
    lat, lon = a["lat"].to_numpy()[:, None], a["lon"].to_numpy()[:, None]
    return haversine_km(lat, lon, b["lat"].to_numpy(), b["lon"].to_numpy())


def load_schools() -> pd.DataFrame:
    if SCHOOLS.exists():
        print(f"schools: reusing {SCHOOLS} (delete it to query Overpass again)")
        return pd.read_csv(SCHOOLS, keep_default_na=False)  # blank names stay blank
    schools, used = delhi_schools()
    df = pd.DataFrame(schools)
    df.to_csv(SCHOOLS, index=False)
    print(f"schools: {len(df):,} from Overpass via the {used!r} query")
    return df


def pick_pilots(schools: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    d = pairwise_km(schools, stations)
    nearest = d.argmin(axis=1)
    cands = schools.assign(
        station_id=stations["location_id"].to_numpy()[nearest],
        station_name=stations["name"].to_numpy()[nearest],
        distance_km=d[np.arange(len(schools)), nearest].round(2),
    )
    cands = cands[
        (cands["distance_km"] <= MAX_KM)
        & cands["name"].map(is_named)  # the alert names the school, so the name must identify it
        & ~cands["station_name"].str.contains(OUTSIDE_DELHI, case=False)  # beside a Delhi monitor
    ]
    pool = cands[cands["name"].str.contains(PREFERRED, case=False)]
    if pool["station_id"].nunique() < N_PILOTS:
        pool = cands  # too few preferred names near distinct stations: take any named school within 2 km
    picked = [pool["distance_km"].idxmin()]  # start with the school closest to its station
    while len(picked) < N_PILOTS:
        rest = pool[~pool["station_id"].isin(pool.loc[picked, "station_id"])]
        if rest.empty:
            break
        picked.append(rest.index[pairwise_km(rest, pool.loc[picked]).min(axis=1).argmax()])  # farthest from picked
    return pool.loc[picked, ["name", "osm_id", "lat", "lon", "station_id", "station_name", "distance_km"]]


def main() -> None:
    schools = load_schools()
    stations = pd.read_csv("data/stations.csv")
    stations = stations[stations["location_id"].isin(model_stations())]
    pilots = pick_pilots(schools, stations)
    pilots = pilots.assign(display_name=pilots["osm_id"].map(DISPLAY_NAMES).fillna(pilots["name"]))
    pilots.to_csv(PILOTS, index=False)
    near = int((pairwise_km(schools, stations).min(axis=1) <= MAX_KM).sum())
    print(f"{len(schools):,} schools, {near:,} within {MAX_KM:.0f} km of one of {len(stations)} model stations")
    print(pilots.to_string(index=False))
    print(f"wrote {SCHOOLS} and {PILOTS}")


if __name__ == "__main__":
    main()
