"""P2-T4: the deployed model, trained on every day to date -> models/clearhour-lgbm.txt and models/features.json."""

import json
from pathlib import Path

import lightgbm as lgb
import pandas as pd
from config import model_stations

from clearhour.constants import IST, TARGET_HOURS
from clearhour.features import FEATURES, build_rows
from clearhour.model import NUM_ROUNDS, fit, predict, with_station_category

MODELS = Path("models")
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
    rows = with_station_category(build_rows(hourly, met, pd.date_range(FIRST_DAY, last_full_day, freq="D")), stations)

    t = rows[rows["target"].notna()]
    by = t.groupby(t["day"].dt.strftime("%Y-%m"))
    check = by.agg(rows=("target", "size"), blh_missing=("blh_t", lambda s: round(float(s.isna().mean()), 3)))
    print(check.to_string())
    if (check["blh_missing"] > 0.10).any():
        raise SystemExit("STOP: blh_missing is above 0.10 in a month")

    booster = fit(rows)
    MODELS.mkdir(exist_ok=True)
    (MODELS / "clearhour-lgbm.txt").write_text(booster.model_to_string())
    meta = {
        "features": FEATURES,
        "stations": stations,
        "trained_through": str(last_full_day.date()),
        "num_rounds": NUM_ROUNDS,
    }
    (MODELS / "features.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"\ntrained on {len(t):,} rows, {len(stations)} stations, {FIRST_DAY.date()} to {last_full_day.date()}")
    imp = pd.Series(booster.feature_importance(importance_type="gain"), index=booster.feature_name())
    print("top 8 features by gain share:")
    print((imp / imp.sum()).sort_values(ascending=False).head(8).round(3).to_string())

    reloaded = lgb.Booster(model_file=str(MODELS / "clearhour-lgbm.txt"))
    day = with_station_category(build_rows(hourly, met, [CHECK_DAY]), stations)
    day = day.assign(pred=predict(reloaded, day))
    names = pd.read_csv("data/stations.csv").set_index("location_id")["name"]
    print(f"\nreloaded model, {CHECK_DAY.date()} (forecast / actual, µg/m³):")
    for station in CHECK_STATIONS:
        r = day[day["location_id"] == station].set_index("target_hour")
        hours = " · ".join(f"{h:02d} {r.loc[h, 'pred']:.0f}/{r.loc[h, 'target']:.0f}" for h in TARGET_HOURS)
        print(f"  {station} {names[station]}: {hours}")
    print(f"wrote {MODELS / 'clearhour-lgbm.txt'} and {MODELS / 'features.json'}")


if __name__ == "__main__":
    main()
