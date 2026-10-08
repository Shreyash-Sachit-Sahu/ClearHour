import pandas as pd
import pytest
from synthetic import make_synthetic

from clearhour import meteo
from clearhour.features import FEATURES, MET_COLUMNS, blackout_for, build_rows
from clearhour.model import evaluate, walk_forward, with_station_category

IST = "Asia/Kolkata"


def test_open_meteo_utc_hours_line_up_with_ist_rows(monkeypatch):
    # Open-Meteo's hours are UTC, i.e. :30 IST; unless fetch labels them with IST hours, every met feature is NaN
    utc = pd.date_range("2025-10-08", "2025-10-13", freq="h", tz="UTC")
    fake = lambda url, params: pd.DataFrame({c: 1.0 for c in params["hourly"].split(",")}, index=utc)  # noqa: E731
    monkeypatch.setattr(meteo, "_hourly", fake)
    met = meteo.fetch("2025-10-08", "2025-10-12")
    hourly, _ = make_synthetic(n_stations=1, end="2025-10-14")
    rows = build_rows(hourly, met, pd.date_range("2025-10-10", "2025-10-11", freq="D", tz=IST))
    assert not rows.empty and rows[["cams_t", "temp_t", "blh_t"]].notna().all().all()


def test_build_rows_rejects_off_hour_met():
    hourly, met = make_synthetic(n_stations=1, end="2025-10-10")
    met.index = met.index + pd.Timedelta(minutes=30)
    with pytest.raises(ValueError, match="met index"):
        build_rows(hourly, met, pd.date_range("2025-10-05", periods=2, freq="D", tz=IST))


def test_build_rows_rejects_off_hour_readings():
    hourly, met = make_synthetic(n_stations=1, end="2025-10-10")
    hourly = hourly.assign(hour_ist=hourly["hour_ist"] + pd.Timedelta(minutes=30))
    with pytest.raises(ValueError, match="hour_ist"):
        build_rows(hourly, met, pd.date_range("2025-10-05", periods=2, freq="D", tz=IST))


def test_rows_have_every_feature_and_sane_leads():
    hourly, met = make_synthetic(n_stations=2, end="2025-11-01")
    days = pd.date_range("2025-10-05", "2025-10-30", freq="D", tz=IST)
    rows = build_rows(hourly, met, days)
    assert list(rows.columns) == ["day", "target", *FEATURES]
    assert set(rows["target_hour"]) == set(range(8, 14))
    assert rows["lead_h"].between(4, 4 + 9 + 6).all()  # 08:00 is 4 h after 04:00; staleness adds up to 6 h
    assert set(MET_COLUMNS) <= set(met.columns)
    # yesterday-same-hour feature really is yesterday's reading at that hour
    one = rows[(rows["location_id"] == 100) & (rows["day"] == pd.Timestamp("2025-10-10", tz=IST))]
    yday = hourly[(hourly["location_id"] == 100) & (hourly["hour_ist"] == pd.Timestamp("2025-10-09 10:00", tz=IST))]
    if not yday.empty:
        assert abs(one.loc[one["target_hour"] == 10, "v_yday_t"].iloc[0] - yday["pm25"].iloc[0]) < 1e-9


def test_walk_forward_beats_persistence_and_reports_decisions():
    hourly, met = make_synthetic()
    days = pd.date_range("2025-10-03", "2026-01-30", freq="D", tz=IST)
    stations = sorted(hourly["location_id"].unique())
    rows = with_station_category(build_rows(hourly, met, days), stations)
    weeks = list(pd.date_range("2025-11-10", "2026-01-26", freq="7D", tz=IST))
    pred = walk_forward(rows, weeks, num_rounds=80)
    assert not pred.empty and pred["pred"].notna().all()
    res = evaluate(pred)
    assert res["mae"]["model"] < res["mae"]["persistence"]
    assert res["station_days"] > 100
    assert 0 <= res["hit_rate_top2"]["model"] <= 1
    assert res["realised_cut_vs_assembly"]["median"] > 0


def test_blackout_hides_late_readings_from_features_but_not_targets():
    hourly, met = make_synthetic(n_stations=2, end="2025-11-01")
    day = pd.Timestamp("2025-10-20", tz=IST)
    fresh = build_rows(hourly, met, [day])
    stale = build_rows(hourly, met, [day], blackout_h=24)
    assert not stale.empty and (stale["lead_h"] >= 4 + 24).all()
    assert stale["v_yday_t"].isna().all()  # yesterday's school hours all fall after 04:00 the day before
    key = ["location_id", "target_hour"]
    both = fresh.merge(stale, on=key, suffixes=("_f", "_s"))
    assert (both["target_f"].fillna(-1) == both["target_s"].fillna(-1)).all()
    upto = hourly[hourly["hour_ist"] <= pd.Timestamp("2025-10-19 11:00", tz=IST)]
    assert blackout_for(upto, day) == 17
    assert blackout_for(hourly, day) == 0
