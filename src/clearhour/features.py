"""Model rows for the 05:30 IST forecast: one row per station, day and school hour (08:00-13:00 IST).

Shared by the backtest, the final training run and the Forecast Lambda, so train and serve can't drift.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from clearhour.constants import IST, TARGET_HOURS

LATEST_OBS_HOUR = 4  # newest bin assumed published by 05:30 IST (the 04:00-05:00 hour); tune from live ingest
MAX_STALENESS_H = 6  # if that bin is missing, fall back at most this many hours
MET_COLUMNS = ["pm2_5", "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "boundary_layer_height"]

FEATURES = [
    "location_id",
    "target_hour",
    "lead_h",
    "dow",
    "v_last",
    "v_last3",
    "v_yday_t",
    "v_yday_school",
    "cams_t",
    "cams_last",
    "temp_t",
    "rh_t",
    "wind_t",
    "blh_t",
    "blh_last",
    "blh_ratio",
]


def _nanmean(a: np.ndarray) -> float:
    a = a[np.isfinite(a)]
    return float(a.mean()) if a.size else float("nan")


def build_rows(hourly: pd.DataFrame, met: pd.DataFrame, days) -> pd.DataFrame:
    """Feature rows for each station x day x school hour.

    hourly: columns location_id, hour_ist (tz-aware, IST, hour-beginning), pm25.
    met: city-point hourly weather and CAMS indexed by tz-aware hour, with MET_COLUMNS.
    days: IST midnights (tz-aware) to build rows for. `target` is NaN where no reading exists.
    """
    if hourly.empty:
        return pd.DataFrame(columns=["day", "target", *FEATURES])
    wide = hourly.pivot_table(index="hour_ist", columns="location_id", values="pm25", aggfunc="mean")
    start = min(wide.index.min(), *days) - pd.Timedelta(days=2)
    end = max(wide.index.max(), *days) + pd.Timedelta(days=1)
    full = pd.date_range(start.floor("D"), end.ceil("D"), freq="h", tz=IST)
    vals = wide.reindex(full).to_numpy(dtype=float)
    metv = met.reindex(full.tz_convert(met.index.tz) if met.index.tz is not None else full)[MET_COLUMNS]
    m = {c: metv[c].to_numpy(dtype=float) for c in MET_COLUMNS}
    pos = {ts: i for i, ts in enumerate(full)}
    stations = wide.columns.to_numpy()

    rows: list[dict] = []
    for day in days:
        d0 = pd.Timestamp(day).tz_convert(IST).normalize()
        i_latest = pos.get(d0 + pd.Timedelta(hours=LATEST_OBS_HOUR))
        if i_latest is None:
            continue
        last_idx = np.full(len(stations), -1)
        for k in range(MAX_STALENESS_H + 1):
            i = i_latest - k
            if i < 0:
                break
            hit = (last_idx < 0) & np.isfinite(vals[i])
            last_idx[hit] = i
        y_idx = [pos.get(d0 - pd.Timedelta(days=1) + pd.Timedelta(hours=t)) for t in TARGET_HOURS]
        t_idx = [pos.get(d0 + pd.Timedelta(hours=t)) for t in TARGET_HOURS]
        for j, station in enumerate(stations):
            li = int(last_idx[j])
            if li < 0:
                continue
            col = vals[:, j]
            yday = np.array([col[i] if i is not None else np.nan for i in y_idx])
            v_yday_school = _nanmean(yday)
            v_last3 = _nanmean(col[max(li - 2, 0) : li + 1])
            blh_last = m["boundary_layer_height"][li]
            for t, yv, it in zip(TARGET_HOURS, yday, t_idx, strict=True):
                if it is None:
                    continue
                blh_t = m["boundary_layer_height"][it]
                rows.append(
                    {
                        "day": d0,
                        "target": col[it],
                        "location_id": station,
                        "target_hour": t,
                        "lead_h": it - li,
                        "dow": d0.dayofweek,
                        "v_last": col[li],
                        "v_last3": v_last3,
                        "v_yday_t": yv,
                        "v_yday_school": v_yday_school,
                        "cams_t": m["pm2_5"][it],
                        "cams_last": m["pm2_5"][li],
                        "temp_t": m["temperature_2m"][it],
                        "rh_t": m["relative_humidity_2m"][it],
                        "wind_t": m["wind_speed_10m"][it],
                        "blh_t": blh_t,
                        "blh_last": blh_last,
                        "blh_ratio": blh_t / max(blh_last, 50.0)
                        if np.isfinite(blh_t) and np.isfinite(blh_last)
                        else np.nan,
                    }
                )
    return pd.DataFrame(rows, columns=["day", "target", *FEATURES])
