"""Model rows for the 05:30 IST forecast: one row per station, day and school hour (08:00-13:00 IST).

Shared by the backtest, the final training run and the Forecast Lambda, so train and serve can't drift.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from clearhour.constants import ASSEMBLY_HOUR, IST, TARGET_HOURS

LATEST_OBS_HOUR = 4  # the 04:00-05:00 IST bin: the newest one that could exist at 05:30
MAX_STALENESS_H = 6  # a station may lag the cutoff by at most this many hours
# OpenAQ publishes Delhi's readings in late batches, so at 05:30 the newest reading can be a day old. The model
# trains on these blackouts (hours of missing readings before 04:00) and serves whichever one the data shows.
BLACKOUTS_H = (0, 6, 12, 18, 24, 30, 36)
MAX_BLACKOUT_H = BLACKOUTS_H[-1]
# One model trained on every blackout lost too much on fresh mornings, so there are two: the fresh model (blackout 0)
# and the stale model (these blackouts). The forecast picks one by the morning's blackout.
STALE_FROM_H = 6
STALE_BLACKOUTS_H = tuple(b for b in BLACKOUTS_H if b >= STALE_FROM_H)
FRESH_LEAD_H = ASSEMBLY_HOUR - LATEST_OBS_HOUR + MAX_STALENESS_H  # 08:00 leads up to this: a reading from the night
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


def _require_on_the_hour(ts: pd.DatetimeIndex, name: str) -> None:
    """The lookups below go by exact hour, so an off-hour timestamp would silently become a NaN feature."""
    off = ts[ts.minute != 0]
    if len(off):
        raise ValueError(f"{name}: {len(off)} timestamps are not on the hour, e.g. {off[0]}")


def blackout_for(hourly: pd.DataFrame, day) -> int:
    """Hours between the 04:00 IST bin and the newest reading at or before it (0 when that bin is in)."""
    cutoff = pd.Timestamp(day).tz_convert(IST).normalize() + pd.Timedelta(hours=LATEST_OBS_HOUR)
    seen = hourly.loc[hourly["hour_ist"] <= cutoff, "hour_ist"]
    if seen.empty:
        return MAX_BLACKOUT_H + 1
    return int((cutoff - seen.max()) / pd.Timedelta(hours=1))


def build_rows(hourly: pd.DataFrame, met: pd.DataFrame, days, blackout_h: int = 0) -> pd.DataFrame:
    """Feature rows for each station x day x school hour.

    hourly: columns location_id, hour_ist (tz-aware, IST, hour-beginning), pm25.
    met: city-point hourly weather and CAMS indexed by tz-aware hour, with MET_COLUMNS.
    days: IST midnights (tz-aware) to build rows for. `target` is NaN where no reading exists.
    blackout_h: readings from the last this-many hours before 04:00 count as not yet published, for the
    latest-reading and yesterday features alike. Targets are never affected.
    """
    if hourly.empty:
        return pd.DataFrame(columns=["day", "target", *FEATURES])
    _require_on_the_hour(met.index, "met index")
    _require_on_the_hour(pd.DatetimeIndex(hourly["hour_ist"]), 'hourly["hour_ist"]')
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
        if i_latest is None or i_latest - blackout_h < 0:
            continue
        i_cut = i_latest - blackout_h  # the newest hour treated as published
        last_idx = np.full(len(stations), -1)
        for k in range(MAX_STALENESS_H + 1):
            i = i_cut - k
            if i < 0:
                break
            hit = (last_idx < 0) & np.isfinite(vals[i])
            last_idx[hit] = i
        y_idx = [pos.get(d0 - pd.Timedelta(days=1) + pd.Timedelta(hours=t)) for t in TARGET_HOURS]
        y_idx = [i if i is not None and i <= i_cut else None for i in y_idx]
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
