"""P2-T3: walk-forward backtest -> outputs/backtest.json and outputs/backtest_by_lead.csv.

Winter: train on every earlier day, test each week from Monday 10 Nov 2025 to Monday 26 Jan 2026 (rows up to
31 Jan 2026). October: train on everything before 1 Oct 2026 and test its first week, the air the live demo runs in.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
from config import model_stations

from clearhour.constants import ASSEMBLY_HOUR, IST, TARGET_HOURS
from clearhour.decide import ASSEMBLY_INDOOR_MIN, decide
from clearhour.features import build_rows
from clearhour.model import evaluate, walk_forward, with_station_category

MONDAYS = list(pd.date_range("2025-11-10", "2026-01-26", freq="7D", tz=IST))
WINTER_END = pd.Timestamp("2026-01-31", tz=IST)
OCTOBER = pd.Timestamp("2026-10-01", tz=IST)
OUT = Path("outputs")

KINDS = ["fine", "clear_hour", "no_window"]
ASSEMBLY_COL = TARGET_HOURS.index(ASSEMBLY_HOUR)


def school_day_matrix(pred: pd.DataFrame) -> dict[str, np.ndarray]:
    """The station-days evaluate() scores (Mon-Fri, all six school hours observed), as arrays with one row per
    station-day and one column per school hour 08..13: the actual value, the model forecast and CAMS.
    "v_last" is one value per station-day: the latest reading the forecast started from."""
    cols = ["target", "pred", "cams_t", "v_last"]
    ok = pred[pred["target"].notna() & (pred["day"].dt.dayofweek < 5)]
    size = ok.groupby(["location_id", "day"], observed=True)["target"].transform("size")
    full = ok[size == len(TARGET_HOURS)]
    wide = full.set_index(["location_id", "day", "target_hour"])[cols].unstack("target_hour")
    m = {c: wide[c][TARGET_HOURS].to_numpy(dtype=float) for c in cols}
    m["v_last"] = m["v_last"][:, ASSEMBLY_COL]
    return m


def rule_decisions(m: dict[str, np.ndarray], use_latest: bool = True) -> list[dict]:
    """The production rule (clearhour.decide) on each station-day's forecast, with or without the latest reading."""
    return [
        decide(dict(zip(TARGET_HOURS, row, strict=True)), latest=float(now) if use_latest else None)
        for row, now in zip(m["pred"], m["v_last"], strict=True)
    ]


def strategy_table(pred: pd.DataFrame) -> pd.DataFrame:
    """Score each way of choosing the outdoor hour on the PM2.5 that actually occurred at the chosen hour.

    oracle is the cleanest school hour in hindsight; model and cams take the hour their forecast calls cleanest.
    clearhour is what the production rule names (on fine and no-window days, which name no hour, the model's pick).
    cut = 1 - PM2.5 at the chosen hour / PM2.5 at assembly (08:00), per station-day. pooled_cut = 1 - mean PM2.5
    at the chosen hour / mean PM2.5 at assembly, so the smoggiest days count the most.
    """
    m = school_day_matrix(pred)
    actual, cams = m["target"], m["cams_t"]
    n = len(actual)
    if n == 0:
        return pd.DataFrame()
    every_day = np.ones(n, dtype=bool)
    model_pick = np.argmin(m["pred"], axis=1)
    rule_pick = np.array(
        [
            TARGET_HOURS.index(d["clear_hour"]) if d["clear_hour"] is not None else p
            for d, p in zip(rule_decisions(m), model_pick, strict=True)
        ]
    )
    choices = {
        "oracle": (np.argmin(actual, axis=1), every_day),
        "model": (model_pick, every_day),
        "clearhour": (rule_pick, every_day),
        "cams": (np.argmin(np.where(np.isnan(cams), np.inf, cams), axis=1), ~np.isnan(cams).any(axis=1)),
        "always_12": (np.full(n, TARGET_HOURS.index(12)), every_day),
        "always_13": (np.full(n, TARGET_HOURS.index(13)), every_day),
    }
    top2 = np.argsort(actual, axis=1, kind="stable")[:, :2]
    rows = []
    for name, (pick, valid) in choices.items():
        i = np.flatnonzero(valid)
        if i.size == 0:
            continue
        got, assembly = actual[i, pick[i]], actual[i, ASSEMBLY_COL]
        pos = assembly > 0
        cut = 1 - got[pos] / assembly[pos]
        rows.append(
            {
                "strategy": name,
                "station_days": len(i),
                "hit_rate_top2": round(float((top2[i] == pick[i, None]).any(axis=1).mean()), 3),
                "median_cut": round(float(np.median(cut)), 3),
                "mean_cut": round(float(cut.mean()), 3),
                "pooled_cut": round(float(1 - got.mean() / assembly.mean()), 3),
                "pm25_at_pick": round(float(got.mean()), 1),
                "pm25_at_assembly": round(float(assembly.mean()), 1),
                "worse_than_assembly": round(float((got > assembly).mean()), 3),
            }
        )
    return pd.DataFrame(rows)


def assembly_flag(pred: pd.DataFrame) -> dict:
    """The 'hold assembly indoors' call (08:00 above ASSEMBLY_INDOOR_MIN) on school days: model vs persistence."""
    a = pred[
        (pred["target_hour"] == ASSEMBLY_HOUR)
        & (pred["day"].dt.dayofweek < 5)
        & pred["target"].notna()
        & pred["v_last"].notna()
    ]
    above = a["target"] > ASSEMBLY_INDOOR_MIN
    out = {"station_days": len(a), "actual_indoor_share": round(float(above.mean()), 3)}
    for name, col in (("model", "pred"), ("persistence", "v_last")):
        said = a[col] > ASSEMBLY_INDOOR_MIN
        out[name] = {
            "accuracy": round(float((said == above).mean()), 3),
            "missed_indoor": int((~said & above).sum()),  # said outdoors, but 08:00 was above the line
            "needless_indoor": int((said & ~above).sum()),
        }
    return out


def kind_table(pred: pd.DataFrame, use_latest: bool = True) -> dict:
    """Station-days by the kind of day the rule called (outer keys) and the kind the air turned out to be (inner)."""
    m = school_day_matrix(pred)
    called = [d["kind"] for d in rule_decisions(m, use_latest)]
    actual = [decide(dict(zip(TARGET_HOURS, row, strict=True)))["kind"] for row in m["target"]]
    table = pd.crosstab(pd.Series(called, name="called"), pd.Series(actual, name="actual"))
    table = table.reindex(index=KINDS, columns=KINDS, fill_value=0)
    return {c: {a: int(table.loc[c, a]) for a in KINDS} for c in KINDS}


def decision_scores(pred: pd.DataFrame) -> dict:
    return {
        "strategies": strategy_table(pred).to_dict(orient="records"),
        "assembly_flag": assembly_flag(pred),
        "kinds": kind_table(pred),
        "kinds_forecast_only": kind_table(pred, use_latest=False),
    }


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


def print_scores(label: str, res: dict) -> None:
    mae = res["mae"]
    print(
        f"\n== {label}: MAE model {mae['model']} · persistence {mae['persistence']} · CAMS {mae['cams']}"
        f" · yesterday {mae['yesterday']}"
    )
    print(pd.DataFrame(res["strategies"]).to_string(index=False))
    flag = res["assembly_flag"]
    print(
        f"assembly flag ({flag['station_days']} station-days, actually above {ASSEMBLY_INDOOR_MIN} on "
        f"{flag['actual_indoor_share']}):"
    )
    for name in ("model", "persistence"):
        print(f"  {name:<11} {flag[name]}")
    tables = {"production rule": res["kinds"], "forecast only": res["kinds_forecast_only"]}
    print("kinds (rows: called, columns: what the air turned out to be):")
    both = pd.concat({k: pd.DataFrame(v).T.reindex(index=KINDS, columns=KINDS) for k, v in tables.items()}, axis=1)
    print(both.to_string())


def main() -> None:
    stations = model_stations()
    hourly = pd.read_parquet("data/processed/pm25_hourly.parquet", columns=["location_id", "hour_ist", "pm25"])
    hourly = hourly[hourly["location_id"].isin(stations)]
    met = pd.read_parquet("data/processed/meteo.parquet")
    days = pd.date_range(pd.Timestamp("2025-10-03", tz=IST), hourly["hour_ist"].max().normalize(), freq="D")
    rows = with_station_category(build_rows(hourly, met, days), stations)
    winter = rows[rows["day"] <= WINTER_END]
    pred = walk_forward(winter, MONDAYS)
    res = evaluate(pred) | decision_scores(pred)

    s = strategy_table(pred).set_index("strategy")
    assert s.loc["model", "station_days"] == res["station_days"]
    for k in ("model", "cams", "always_12", "always_13"):
        assert s.loc[k, "hit_rate_top2"] == res["hit_rate_top2"][k], k
    for k in ("median", "mean"):
        assert abs(s.loc["model", f"{k}_cut"] - res["realised_cut_vs_assembly"][k]) <= 0.001, k

    oct_pred = walk_forward(rows, [OCTOBER])
    october = evaluate(oct_pred) | decision_scores(oct_pred)
    october["test_days"] = sorted({d.date().isoformat() for d in oct_pred["day"]})
    res["october_2026"] = october

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
    print_scores("Winter 2025-26", res)
    print_scores(f"October 2026 ({', '.join(october['test_days'])}; {october['rows']} test rows)", october)
    print("\nwrote outputs/backtest.json and outputs/backtest_by_lead.csv")

    stops = []
    if mae["model"] >= mae["yesterday"]:
        stops.append("the model's MAE is not below yesterday-same-hour's")
    if hit["model"] < hit["always_13"] + 0.02:
        stops.append("the model's hit rate is less than 0.02 above always_13's")
    pooled = pd.DataFrame(res["strategies"]).set_index("strategy")["pooled_cut"]
    if pooled["clearhour"] < pooled["model"] - 0.01:
        stops.append("the production rule's pooled cut is more than 0.01 below the model's")
    if stops:
        raise SystemExit("STOP: " + "; ".join(stops))


if __name__ == "__main__":
    main()
