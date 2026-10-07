import gzip
import io

import numpy as np
import pandas as pd

from clearhour.archive import read_pm25, to_hourly
from clearhour.hook import hook_numbers, weekday_profile


def _diurnal(hour_ist: int) -> float:
    # Synthetic Delhi-like winter cycle: high overnight and at 8 AM, lowest mid-afternoon.
    return 150 + 90 * np.cos((hour_ist - 8) / 24 * 2 * np.pi) - 40 * np.exp(-((hour_ist - 15) ** 2) / 8)


def _archive_csv(location_id: int, day: pd.Timestamp) -> bytes:
    """One synthetic daily archive file, stamped at the END of each hour, with offset timestamps."""
    rows = []
    for h in range(24):
        end_ist = (day + pd.Timedelta(hours=h + 1)).tz_localize("Asia/Kolkata")
        rows.append(
            {
                "location_id": location_id,
                "sensors_id": location_id * 10,
                "location": f"Station {location_id}",
                "datetime": end_ist.isoformat(),
                "lat": 28.6,
                "lon": 77.2,
                "parameter": "pm25",
                "units": "µg/m³",
                "value": _diurnal(h),
            }
        )
        rows.append({**rows[-1], "parameter": "pm10", "value": 999.0})
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(pd.DataFrame(rows).to_csv(index=False).encode())
    return buf.getvalue()


def test_end_stamped_files_land_in_the_right_ist_hour(tmp_path):
    frames = []
    for loc in (1, 2, 3):
        for day in pd.date_range("2025-11-01", "2025-12-31", freq="D"):
            p = tmp_path / f"location-{loc}-{day:%Y%m%d}.csv.gz"
            p.write_bytes(_archive_csv(loc, day))
            frames.append(read_pm25(p))
    hourly = to_hourly(pd.concat(frames, ignore_index=True))

    # pm10 rows are filtered out and every station-hour appears once
    assert hourly["pm25"].max() < 300
    assert hourly.groupby(["location_id", "hour_utc"]).size().max() == 1

    profile, n_stations = weekday_profile(hourly, "2025-11-01", "2026-01-01")
    assert n_stations == 3
    for h in range(24):
        assert abs(profile.loc[h] - _diurnal(h)) < 1e-6  # end-stamped 09:00 reading -> 08:00 bin

    nums = hook_numbers(profile)
    assert nums["assembly_hour_ist"] == "08:00-09:00"
    assert nums["cleanest_school_hour_ist"] == "13:00-14:00"
    assert 0 < nums["cut_pct"] < 100


def test_quarter_hour_readings_bin_to_ist_clock_hours():
    # Real archive data is 15-minute and end-stamped; IST is UTC+5:30, so UTC hour bins would straddle IST hours.
    stamps = pd.date_range("2025-11-12 08:15", periods=4, freq="15min", tz="Asia/Kolkata")
    readings = pd.DataFrame({"location_id": 1, "ts_utc": stamps.tz_convert("UTC"), "value": [100.0] * 3 + [200.0]})
    hourly = to_hourly(readings)
    assert len(hourly) == 1
    assert hourly["hour_ist"].iloc[0] == pd.Timestamp("2025-11-12 08:00", tz="Asia/Kolkata")
    assert hourly["n"].iloc[0] == 4 and hourly["pm25"].iloc[0] == 125.0


def test_start_stamped_hourly_reading_lands_in_its_own_hour():
    # The US Embassy monitor (AirNow) stamps each hourly reading at the start of the hour it covers.
    stamp = pd.Timestamp("2025-11-12 08:00", tz="Asia/Kolkata")
    readings = pd.DataFrame({"location_id": [8118], "ts_utc": [stamp.tz_convert("UTC")], "value": [150.0]})
    assert to_hourly(readings, stamped_at_end=False)["hour_ist"].tolist() == [stamp]


def test_low_coverage_stations_are_dropped(tmp_path):
    frames = []
    for day in pd.date_range("2025-11-01", "2025-12-31", freq="D"):
        p = tmp_path / f"a-{day:%Y%m%d}.csv.gz"
        p.write_bytes(_archive_csv(1, day))
        frames.append(read_pm25(p))
    for day in pd.date_range("2025-11-01", "2025-11-10", freq="D"):  # ~16% coverage
        p = tmp_path / f"b-{day:%Y%m%d}.csv.gz"
        p.write_bytes(_archive_csv(2, day))
        frames.append(read_pm25(p))
    _, n_stations = weekday_profile(to_hourly(pd.concat(frames, ignore_index=True)), "2025-11-01", "2026-01-01")
    assert n_stations == 1
