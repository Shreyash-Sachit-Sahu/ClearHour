"""Synthetic Delhi-like data shared by the tests: morning peak, afternoon dip, 5% missing hours."""

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"


def make_synthetic(n_stations: int = 6, start: str = "2025-10-01", end: str = "2026-01-31", seed: int = 3):
    """Hourly station PM2.5 and a city-point met frame with a realistic morning peak and afternoon dip."""
    rng = np.random.default_rng(seed)
    hours = pd.date_range(start, end, freq="h", tz=IST, inclusive="left")
    h = hours.hour.to_numpy()
    blh = 150 + 900 * np.clip(np.sin((h - 7) / 11 * np.pi), 0, None)  # low at night, peak mid-afternoon
    day_level = np.repeat(rng.normal(0, 0.25, len(hours) // 24 + 1).cumsum() * 0.2, 24)[: len(hours)]
    city = np.exp(np.log(180) + day_level) * (1.4 - 0.9 * (blh - 150) / 900)
    met = pd.DataFrame(
        {
            "pm2_5": city * rng.normal(1.0, 0.15, len(hours)),
            "temperature_2m": 18 + 8 * np.sin((h - 9) / 24 * 2 * np.pi),
            "relative_humidity_2m": 70 - 20 * np.sin((h - 9) / 24 * 2 * np.pi),
            "wind_speed_10m": 4 + rng.normal(0, 1, len(hours)).clip(-3, 3),
            "boundary_layer_height": blh,
        },
        index=hours,
    )
    frames = []
    for s in range(n_stations):
        noise = rng.normal(1.0, 0.08, len(hours))
        v = city * (0.8 + 0.1 * s) * noise
        keep = rng.random(len(hours)) > 0.05  # 5% missing hours
        frames.append(pd.DataFrame({"location_id": 100 + s, "hour_ist": hours[keep], "pm25": v[keep]}))
    return pd.concat(frames, ignore_index=True), met
