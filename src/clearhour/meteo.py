"""City-point weather and CAMS PM2.5 from Open-Meteo (no key; credit CAMS and Open-Meteo in the README).

One central-Delhi point is enough: CAMS is a ~45 km grid and the mixing-layer signal is city-wide.
Station-to-station differences come from each station's own recent readings.
"""

from __future__ import annotations

import pandas as pd
import requests

CITY = (28.6139, 77.2090)
WEATHER_ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"  # stitched past forecasts
WEATHER_LIVE = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"
WEATHER_VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "boundary_layer_height"]


def _hourly(url: str, params: dict) -> pd.DataFrame:
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    h = r.json()["hourly"]
    df = pd.DataFrame(h)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")


def fetch(start_date: str, end_date: str, *, live: bool = False) -> pd.DataFrame:
    """Hourly met for [start_date, end_date] (inclusive, YYYY-MM-DD), indexed by IST hour."""
    base = {
        "latitude": CITY[0],
        "longitude": CITY[1],
        "timezone": "GMT",
        "start_date": start_date,
        "end_date": end_date,
    }
    weather = _hourly(WEATHER_LIVE if live else WEATHER_ARCHIVE, {**base, "hourly": ",".join(WEATHER_VARS)})
    aq = _hourly(AIR_QUALITY, {**base, "hourly": "pm2_5", "domains": "cams_global"})
    met = weather.join(aq, how="outer")
    met.index = met.index.tz_convert("Asia/Kolkata")
    for col in [*WEATHER_VARS, "pm2_5"]:
        if col not in met:
            met[col] = float("nan")
    return met.astype(float)
