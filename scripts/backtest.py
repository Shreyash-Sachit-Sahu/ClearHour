"""P2-T3: walk-forward backtest -> outputs/backtest.json and outputs/backtest_by_lead.csv."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
from config import model_stations

from clearhour.constants import IST, TARGET_HOURS
from clearhour.features import build_rows
from clearhour.model import evaluate, walk_forward, with_station_category

DAYS = pd.date_range("2025-10-03", "2026-01-31", freq="D", tz=IST)
MONDAYS = list(pd.date_range("2025-11-10", "2026-01-26", freq="7D", tz=IST))
OUT = Path("outputs")


def against_always_13(pred: pd.DataFrame) -> dict:
    """On evaluate()'s station-days (weekdays, all six school hours observed): does the model add anything
    where 13:00 is not among the two cleanest hours, and how often does it simply pick 13:00?"""
    ok = pred[pred["target"].notna() & (pred["day"].dt.dayofweek < 5)]
    full = ok.groupby(["location_id", "day"], observed=True).filter(lambda g: len(g) == len(TARGET_HOURS))
    hits_when_13_misses, picks_13 = [], []
    for _, g in full.groupby(["location_id", "day"], observed=True):
        g = g.set_index("target_hour")
        best_two = set(g["target"].nsmallest(2).index)
        pick = int(g["pred"].idxmin())
        picks_13.append(pick == 13)
        if 13 not in best_two:
            hits_when_13_misses.append(pick in best_two)
    return {
        "station_days_where_always_13_misses": len(hits_when_13_misses),
        "model_hit_rate_top2_where_always_13_misses": round(float(np.mean(hits_when_13_misses)), 3),
        "share_model_picks_13": round(float(np.mean(picks_13)), 3),
    }


def main() -> None:
    stations = model_stations()
    hourly = pd.read_parquet("data/processed/pm25_hourly.parquet", columns=["location_id", "hour_ist", "pm25"])
    hourly = hourly[hourly["location_id"].isin(stations)]
    met = pd.read_parquet("data/processed/meteo.parquet")
    rows = with_station_category(build_rows(hourly, met, DAYS), stations)
    pred = walk_forward(rows, MONDAYS)
    res = evaluate(pred)
    extra = against_always_13(pred)
    weeks = sorted(str(w.date()) for w in pred["week"].unique())
    result = {"stations": len(stations), "station_ids": stations, "test_weeks": weeks, **res, **extra}
    (OUT / "backtest.json").write_text(json.dumps(result, indent=2) + "\n")
    pd.DataFrame(res["mae_by_lead"]).to_csv(OUT / "backtest_by_lead.csv", index=False)

    mae, hit = res["mae"], res["hit_rate_top2"]
    print(f"{len(stations)} stations, {len(weeks)} test weeks ({weeks[0]} to {weeks[-1]}), {res['rows']:,} test rows")
    print(
        f"MAE (µg/m³): model {mae['model']} · persistence {mae['persistence']} · CAMS {mae['cams']}"
        f" · yesterday {mae['yesterday']}"
    )
    print(
        f"top-2 hit rate ({res['station_days']:,} station-days): model {hit['model']} · always_13 {hit['always_13']}"
        f" · always_12 {hit['always_12']} · CAMS {hit['cams']}"
    )
    print(f"realised cut vs assembly: median {res['realised_cut_vs_assembly']['median']}")
    print(
        f"where always_13 misses ({extra['station_days_where_always_13_misses']:,} station-days): model hit rate "
        f"{extra['model_hit_rate_top2_where_always_13_misses']} · model picks 13:00 on {extra['share_model_picks_13']}"
        " of all station-days"
    )
    print("wrote outputs/backtest.json and outputs/backtest_by_lead.csv")

    stops = []
    if mae["model"] >= mae["yesterday"]:
        stops.append("the model's MAE is not below yesterday-same-hour's")
    if hit["model"] < hit["always_13"] + 0.02:
        stops.append("the model's hit rate is less than 0.02 above always_13's")
    if stops:
        raise SystemExit("STOP: " + "; ".join(stops))


if __name__ == "__main__":
    main()
