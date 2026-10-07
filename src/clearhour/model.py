"""LightGBM training, walk-forward backtest and the decision metrics."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd

from clearhour.constants import TARGET_HOURS
from clearhour.features import FEATURES

PARAMS = {
    "objective": "regression",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 40,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.9,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "seed": 7,
    "deterministic": True,
    "force_row_wise": True,
    "verbose": -1,
}
NUM_ROUNDS = 400


def with_station_category(rows: pd.DataFrame, stations: list) -> pd.DataFrame:
    """Fix the station categories so codes match across folds and at serving time."""
    return rows.assign(location_id=pd.Categorical(rows["location_id"], categories=stations))


def fit(train: pd.DataFrame, num_rounds: int = NUM_ROUNDS) -> lgb.Booster:
    train = train[train["target"].notna()]
    data = lgb.Dataset(train[FEATURES], label=np.log1p(train["target"]), categorical_feature=["location_id"])
    return lgb.train(PARAMS, data, num_boost_round=num_rounds)


def predict(model: lgb.Booster, rows: pd.DataFrame) -> np.ndarray:
    return np.expm1(model.predict(rows[FEATURES]))


def walk_forward(rows: pd.DataFrame, week_starts: list[pd.Timestamp], num_rounds: int = NUM_ROUNDS) -> pd.DataFrame:
    """For each test week: train on every earlier day, predict that week. Returns the test rows with `pred`."""
    out = []
    for wk in week_starts:
        train = rows[rows["day"] < wk]
        test = rows[(rows["day"] >= wk) & (rows["day"] < wk + pd.Timedelta(days=7))]
        if train["target"].notna().sum() == 0 or test.empty:
            continue
        model = fit(train, num_rounds)
        out.append(test.assign(pred=predict(model, test), week=wk))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def evaluate(pred: pd.DataFrame) -> dict:
    """Forecast error and decision quality on school days (Mon-Fri) with all six school hours observed."""
    ok = pred[pred["target"].notna()]

    def mae(col: str) -> float:
        valid = ok[ok[col].notna()]
        return round(float((valid[col] - valid["target"]).abs().mean()), 1)

    result = {
        "rows": int(len(ok)),
        "mae": {
            "model": mae("pred"),
            "persistence": mae("v_last"),
            "cams": mae("cams_t"),
            "yesterday": mae("v_yday_t"),
        },
    }

    days = ok[ok["day"].dt.dayofweek < 5]
    full = days.groupby(["location_id", "day"], observed=True).filter(lambda g: len(g) == len(TARGET_HOURS))
    hits = {"model": [], "cams": [], "always_12": [], "always_13": []}
    cuts: list[float] = []
    for _, g in full.groupby(["location_id", "day"], observed=True):
        g = g.set_index("target_hour")
        best_two = set(g["target"].nsmallest(2).index)
        hits["model"].append(int(g["pred"].idxmin()) in best_two)
        if g["cams_t"].notna().all():
            hits["cams"].append(int(g["cams_t"].idxmin()) in best_two)
        hits["always_12"].append(12 in best_two)
        hits["always_13"].append(13 in best_two)
        assembly = g.loc[TARGET_HOURS[0], "target"]
        if assembly > 0:
            cuts.append((assembly - g.loc[int(g["pred"].idxmin()), "target"]) / assembly)
    result["station_days"] = len(hits["model"])
    result["hit_rate_top2"] = {k: round(float(np.mean(v)), 3) if v else None for k, v in hits.items()}
    result["realised_cut_vs_assembly"] = {
        "median": round(float(np.median(cuts)), 3) if cuts else None,
        "mean": round(float(np.mean(cuts)), 3) if cuts else None,
    }
    by_lead = ok.assign(err_model=(ok["pred"] - ok["target"]).abs(), err_persist=(ok["v_last"] - ok["target"]).abs())
    result["mae_by_lead"] = (
        by_lead.groupby("lead_h")[["err_model", "err_persist"]].mean().round(1).reset_index().to_dict("records")
    )
    return result
