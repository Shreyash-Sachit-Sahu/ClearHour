"""P2-T4 / Phase 2d: the deployed models, trained on every day to date.

The fresh model (blackout 0) serves mornings that have the night's readings; the stale model (STALE_BLACKOUTS_H)
serves mornings that don't. Writes models/clearhour-lgbm{,-stale}.txt and models/features{,-stale}.json.
"""

import json
from pathlib import Path

import lightgbm as lgb
import pandas as pd
from config import model_stations

from clearhour.constants import IST, TARGET_HOURS
from clearhour.features import FEATURES, STALE_BLACKOUTS_H, build_rows
from clearhour.model import NUM_ROUNDS, fit, predict, with_station_category

MODELS = Path("models")
KINDS = {  # model -> (booster file, features file, blackouts it trains on)
    "fresh": ("clearhour-lgbm.txt", "features.json", (0,)),
    "stale": ("clearhour-lgbm-stale.txt", "features-stale.json", STALE_BLACKOUTS_H),
}
FIRST_DAY = pd.Timestamp("2025-10-03", tz=IST)
CHECK_DAY = pd.Timestamp("2025-11-13", tz=IST)  # the replay morning of P2-T7
CHECK_STATIONS = [235, 8118]  # Anand Vihar (CPCB) and the US Embassy (AirNow)


def main() -> None:
    stations = model_stations()
    hourly = pd.read_parquet("data/processed/pm25_hourly.parquet", columns=["location_id", "hour_ist", "pm25"])
    hourly = hourly[hourly["location_id"].isin(stations)]
    met = pd.read_parquet("data/processed/meteo.parquet")
    last_full_day = (hourly["hour_ist"].max() + pd.Timedelta(hours=1)).normalize() - pd.Timedelta(days=1)
    if met.index.max().normalize() < last_full_day:
        raise SystemExit("meteo.parquet ends before the last day of PM2.5: re-run scripts/fetch_meteo.py first")
    days = pd.date_range(FIRST_DAY, last_full_day, freq="D")
    all_blackouts = sorted({b for spec in KINDS.values() for b in spec[2]})
    rows = {b: with_station_category(build_rows(hourly, met, days, blackout_h=b), stations) for b in all_blackouts}

    t = rows[0][rows[0]["target"].notna()]
    by = t.groupby(t["day"].dt.strftime("%Y-%m"))
    check = by.agg(rows=("target", "size"), blh_missing=("blh_t", lambda s: round(float(s.isna().mean()), 3)))
    print(check.to_string())
    if (check["blh_missing"] > 0.10).any():
        raise SystemExit("STOP: blh_missing is above 0.10 in a month")

    MODELS.mkdir(exist_ok=True)
    for kind, (booster_file, meta_file, blackouts) in KINDS.items():
        train = pd.concat([rows[b] for b in blackouts], ignore_index=True)
        booster = fit(train)
        (MODELS / booster_file).write_text(booster.model_to_string())
        meta = {
            "features": FEATURES,
            "stations": stations,
            "trained_through": str(last_full_day.date()),
            "num_rounds": NUM_ROUNDS,
            "blackouts_h": list(blackouts),
        }
        (MODELS / meta_file).write_text(json.dumps(meta, indent=2) + "\n")
        per = {b: int(rows[b]["target"].notna().sum()) for b in blackouts}
        print(
            f"\n{kind} model: {sum(per.values()):,} rows {per}, {len(stations)} stations, "
            f"{FIRST_DAY.date()} to {last_full_day.date()}"
        )
        imp = pd.Series(booster.feature_importance(importance_type="gain"), index=booster.feature_name())
        print("top 8 features by gain share:")
        print((imp / imp.sum()).sort_values(ascending=False).head(8).round(3).to_string())

    reloaded = lgb.Booster(model_file=str(MODELS / KINDS["fresh"][0]))
    day = with_station_category(build_rows(hourly, met, [CHECK_DAY]), stations)
    day = day.assign(pred=predict(reloaded, day))
    names = pd.read_csv("data/stations.csv").set_index("location_id")["name"]
    print(f"\nreloaded fresh model, {CHECK_DAY.date()} (forecast / actual, µg/m³):")
    for station in CHECK_STATIONS:
        r = day[day["location_id"] == station].set_index("target_hour")
        hours = " · ".join(f"{h:02d} {r.loc[h, 'pred']:.0f}/{r.loc[h, 'target']:.0f}" for h in TARGET_HOURS)
        print(f"  {station} {names[station]}: {hours}")
    print(f"wrote {', '.join(str(MODELS / f) for spec in KINDS.values() for f in spec[:2])}")


if __name__ == "__main__":
    main()
