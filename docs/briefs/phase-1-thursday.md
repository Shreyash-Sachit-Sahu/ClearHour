# Phase 1 brief: Thursday, Oct 8

**Data, hook stat, schools, AWS skeleton.** Read `CLAUDE.md` first; its rules apply to every task here.

**Tonight's gate:**
- `outputs/hook_chart.png` and `outputs/hook_stat.json` built from real data.
- Station coverage is known.
- Five pilot schools are picked.
- The SAM stack is deployed, with one Lambda writing to DynamoDB.

WhatsApp onboarding runs by hand in parallel (Appendix A). It is not a Claude Code task.

## Ground rules for this phase

- Do the tasks in order. After each one, run its check, commit, then stop and show me the output.
- Files marked **Verbatim** passed ruff, pytest on pandas 3.0.6 and cfn-lint before handover. Create them
  exactly as written.
- The S3 archive, the OpenAQ API and Overpass could not be reached during testing. Their code is untested
  against live data, so the task checks below are the real test.
- Anything left open: pick the simplest option, then log it in `docs/DECISIONS.md`.

---

## T1: Scaffold the repo

```bash
mkdir -p ~/code/clearhour && cd ~/code/clearhour && git init -b main
uv init --package --python 3.13
uv add "pandas>=3.0,<3.1" "numpy>=2,<3" pyarrow "matplotlib>=3.11,<3.12" "lightgbm>=4.7,<4.8" \
       "scikit-learn>=1.9,<1.10" requests boto3 openaq
uv add --dev pytest ruff cfn-lint
```

Add to `pyproject.toml`:

```toml
[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Create:

- `.gitignore`, covering: `.venv/`, `__pycache__/`, `.env`, `.aws-sam/`, `data/raw/`, `data/processed/`, `*.part`.
- `.env.example`, with these three lines:

```
OPENAQ_API_KEY=
AWS_PROFILE=clearhour
AWS_REGION=ap-south-1
```

- `docs/DECISIONS.md`, a heading and nothing else yet.
- `README.md`, with section stubs: Problem, How it works, Architecture, Data and credits, AI tools used, Run it.
  Under AI tools used, write: "Claude (planning and design briefs) and Claude Code (implementation)."
- `docs/briefs/`. Put this brief there.

**Check:**

```bash
uv run python -c "import pandas, lightgbm, matplotlib; print(pandas.__version__, lightgbm.__version__, matplotlib.__version__)"
uv run ruff check
```

If `import lightgbm` fails with `libgomp.so.1`, run `sudo apt install -y libgomp1`.
Commit: `chore: scaffold repo`.

## T2: Library code and tests (Verbatim)

Create these files exactly. They are shared by every later phase.

### `src/clearhour/archive.py` (Verbatim)

```python
"""OpenAQ archive on AWS: public bucket, read anonymously."""

from __future__ import annotations

from pathlib import Path

import boto3
import pandas as pd
from botocore import UNSIGNED
from botocore.config import Config

BUCKET = "openaq-data-archive"
IST = "Asia/Kolkata"

_s3 = boto3.client(
    "s3",
    region_name="us-east-1",
    config=Config(signature_version=UNSIGNED, retries={"max_attempts": 10, "mode": "adaptive"}),
)


def month_prefix(location_id: int, year: int, month: int) -> str:
    return f"records/csv.gz/locationid={location_id}/year={year}/month={month:02d}/"


def list_month_keys(location_id: int, year: int, month: int) -> list[str]:
    """Every daily file for one location-month. Lists the prefix rather than guessing file names."""
    keys: list[str] = []
    kwargs = {"Bucket": BUCKET, "Prefix": month_prefix(location_id, year, month)}
    while True:
        resp = _s3.list_objects_v2(**kwargs)
        keys += [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".csv.gz")]
        if not resp.get("IsTruncated"):
            return keys
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]


def fetch(key: str, raw_dir: Path) -> Path:
    """Download once; later runs read the cached copy."""
    dest = raw_dir / key
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        _s3.download_file(BUCKET, key, str(part))
        part.rename(dest)
    return dest


def read_pm25(path: Path) -> pd.DataFrame:
    """One daily file -> PM2.5 rows with a UTC timestamp.

    The docs' column table says sensor_id/unit while sample files say sensors_id/units,
    so names are normalised before use.
    """
    df = pd.read_csv(path, compression="gzip")
    df = df.rename(columns={"sensors_id": "sensor_id", "units": "unit"})
    df = df[df["parameter"].astype(str).str.lower().isin(["pm25", "pm2.5"])]
    return pd.DataFrame(
        {
            "location_id": df["location_id"].astype("int64"),
            "ts_utc": pd.to_datetime(df["datetime"], utc=True, format="ISO8601"),
            "value": pd.to_numeric(df["value"], errors="coerce"),
        }
    )


def to_hourly(readings: pd.DataFrame, *, stamped_at_end: bool = True, valid_max: float = 1000.0) -> pd.DataFrame:
    """Hourly mean PM2.5 per location, keyed by the hour the reading covers (hour-beginning).

    stamped_at_end=True means a value stamped 09:00 covers 08:00-09:00 and lands in the 08:00 hour.
    Confirm the convention with scripts/check_timestamps.py and record it in docs/DECISIONS.md.
    """
    r = readings.dropna(subset=["value"])
    r = r[(r["value"] >= 0) & (r["value"] <= valid_max)]
    shift = pd.Timedelta(minutes=1) if stamped_at_end else pd.Timedelta(0)
    hourly = (
        r.assign(hour_utc=(r["ts_utc"] - shift).dt.floor("h"))
        .groupby(["location_id", "hour_utc"], as_index=False)
        .agg(pm25=("value", "mean"), n=("value", "size"))
    )
    hourly["hour_ist"] = hourly["hour_utc"].dt.tz_convert(IST)
    return hourly
```

### `src/clearhour/openaq_api.py` (Verbatim)

```python
"""OpenAQ v3 API: find Delhi's reference PM2.5 monitors."""

from __future__ import annotations

import os

import requests

API = "https://api.openaq.org/v3"
# Delhi NCT as min lon, min lat, max lon, max lat (OpenAQ accepts up to 4 decimals)
DELHI_BBOX = "76.8380,28.4040,77.3470,28.8830"


def get(path: str, params: dict) -> dict:
    r = requests.get(
        f"{API}{path}",
        params=params,
        headers={"X-API-Key": os.environ["OPENAQ_API_KEY"]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def delhi_pm25_monitors() -> list[dict]:
    """Reference monitors (monitor=true) inside Delhi that carry a PM2.5 sensor."""
    rows: list[dict] = []
    page = 1
    while True:
        results = get(
            "/locations",
            {"bbox": DELHI_BBOX, "monitor": "true", "limit": 100, "page": page},
        )["results"]
        for loc in results:
            pm25 = [s for s in loc.get("sensors", []) if s["parameter"]["name"] == "pm25"]
            if not pm25:
                continue
            rows.append(
                {
                    "location_id": loc["id"],
                    "name": loc["name"],
                    "lat": loc["coordinates"]["latitude"],
                    "lon": loc["coordinates"]["longitude"],
                    "provider": (loc.get("provider") or {}).get("name"),
                    "owner": (loc.get("owner") or {}).get("name"),
                    "pm25_sensor_id": pm25[0]["id"],
                    "first_utc": (loc.get("datetimeFirst") or {}).get("utc"),
                    "last_utc": (loc.get("datetimeLast") or {}).get("utc"),
                }
            )
        if len(results) < 100:
            return rows
        page += 1
```

### `src/clearhour/hook.py` (Verbatim)

```python
"""The hook stat: how much dirtier the assembly hour is than the cleanest school hour."""

from __future__ import annotations

import pandas as pd

IST = "Asia/Kolkata"
SCHOOL_HOURS = list(range(8, 14))  # hour-beginning bins, 08:00 to 13:00 IST
ASSEMBLY_HOUR = 8  # the 08:00-09:00 IST hour


def weekday_profile(
    hourly: pd.DataFrame, start: str, end: str, *, min_coverage: float = 0.6
) -> tuple[pd.Series, int]:
    """Mean PM2.5 by IST hour of day over Mon-Fri in [start, end), each station weighted equally.

    Stations with less than min_coverage of the window's weekday hours are left out.
    Returns the 24-hour profile and the number of stations used.
    """
    lo, hi = pd.Timestamp(start, tz=IST), pd.Timestamp(end, tz=IST)
    h = hourly[(hourly["hour_ist"] >= lo) & (hourly["hour_ist"] < hi)]
    h = h[h["hour_ist"].dt.dayofweek < 5]
    window = pd.date_range(lo, hi, freq="h", inclusive="left")
    expected = int((window.dayofweek < 5).sum())
    coverage = h.groupby("location_id").size() / expected
    keep = coverage[coverage >= min_coverage].index
    h = h[h["location_id"].isin(keep)]
    by_station = h.groupby(["location_id", h["hour_ist"].dt.hour])["pm25"].mean()
    profile = by_station.groupby(level=1).mean()
    profile.index.name = "hour_ist"
    return profile, len(keep)


def hook_numbers(profile: pd.Series) -> dict:
    school = profile.loc[SCHOOL_HOURS]
    assembly = float(profile.loc[ASSEMBLY_HOUR])
    best_hour = int(school.idxmin())
    best = float(school.min())
    return {
        "assembly_hour_ist": f"{ASSEMBLY_HOUR:02d}:00-{ASSEMBLY_HOUR + 1:02d}:00",
        "assembly_pm25": round(assembly, 1),
        "cleanest_school_hour_ist": f"{best_hour:02d}:00-{best_hour + 1:02d}:00",
        "cleanest_pm25": round(best, 1),
        "cut_pct": round((assembly - best) / assembly * 100, 1),
        "assembly_vs_cleanest_ratio": round(assembly / best, 2),
    }
```

### `src/clearhour/hook_chart.py` (Verbatim)

```python
"""The opening chart of the demo video: mean PM2.5 by hour, assembly hour vs the cleanest school hour."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from clearhour.hook import ASSEMBLY_HOUR, SCHOOL_HOURS  # noqa: E402

SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE, SERIES = "#e1e0d9", "#c3c2b7", "#2a78d6"
CRITICAL, GOOD = "#d03b3b", "#0ca30c"  # status colours; each marker also carries a text label


def _clock(h: int) -> str:
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


def draw(profile: pd.Series, nums: dict, n_stations: int, period: str, out: Path) -> None:
    hours = list(range(24))
    x = [h + 0.5 for h in hours]  # plot each hour at the middle of its bin
    y = [float(profile.loc[h]) for h in hours]
    best = int(nums["cleanest_school_hour_ist"][:2])

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    ax.axvspan(SCHOOL_HOURS[0], SCHOOL_HOURS[-1] + 1, color=GRID, alpha=0.55, lw=0, zorder=0)
    top = max(y) * 1.18
    ax.text(SCHOOL_HOURS[0] + 0.15, top * 0.97, "School hours", color=INK_2, fontsize=8.5, va="top")

    ax.plot(x, y, color=SERIES, lw=2.2, solid_capstyle="round", solid_joinstyle="round", zorder=3)
    for hour, colour in ((ASSEMBLY_HOUR, CRITICAL), (best, GOOD)):
        ax.plot(hour + 0.5, float(profile.loc[hour]), "o", ms=8, mfc=colour, mec=SURFACE, mew=2, zorder=4)

    a_val, b_val = float(profile.loc[ASSEMBLY_HOUR]), float(profile.loc[best])
    ax.annotate(
        f"Assembly, {_clock(ASSEMBLY_HOUR)}–{_clock(ASSEMBLY_HOUR + 1)}\n{a_val:.0f} µg/m³",
        (ASSEMBLY_HOUR + 0.5, a_val), xytext=(-14, 10), textcoords="offset points",
        ha="right", va="bottom", fontsize=9, color=INK,
    )
    ax.annotate(
        f"Cleanest school hour, {_clock(best)}–{_clock(best + 1)}\n{b_val:.0f} µg/m³",
        (best + 0.5, b_val), xytext=(14, 12), textcoords="offset points",
        ha="left", va="bottom", fontsize=9, color=INK,
    )

    ax.set_xlim(0, 24)
    ax.set_ylim(0, top)
    ax.set_xticks(range(0, 25, 3), [_clock(h % 24) for h in range(0, 25, 3)])
    ax.tick_params(colors=MUTED, labelsize=8.5, length=0)
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)

    fig.text(0.04, 0.95, f"Moving outdoor time from {_clock(ASSEMBLY_HOUR)} to {_clock(best)} cuts PM2.5 by "
             f"{nums['cut_pct']:.0f}%", fontsize=13, fontweight="semibold", color=INK, va="top")
    fig.text(0.04, 0.885, f"Mean PM2.5 by hour of day (µg/m³) · {n_stations} Delhi monitors · school days, {period}",
             fontsize=9, color=INK_2, va="top")
    fig.text(0.04, 0.03, "Source: Delhi reference monitors via OpenAQ (AWS Open Data). Hourly means, weekdays only.",
             fontsize=7.5, color=MUTED)
    fig.subplots_adjust(left=0.07, right=0.97, top=0.80, bottom=0.14)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
```

### `src/clearhour/overpass.py` (Verbatim)

```python
"""Delhi schools from OpenStreetMap via the Overpass API."""

from __future__ import annotations

import requests

OVERPASS = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "clearhour-hackathon/0.1 (Environmental Hacks 2026)"}

BY_AREA = """
[out:json][timeout:180];
area["ISO3166-2"="IN-DL"]["admin_level"="4"]->.dl;
nwr["amenity"="school"](area.dl);
out center tags;
"""

# Fallback if the area lookup returns nothing: Delhi's bounding box as (south, west, north, east)
BY_BBOX = """
[out:json][timeout:180];
nwr["amenity"="school"](28.404,76.838,28.883,77.347);
out center tags;
"""


def _run(query: str) -> list[dict]:
    r = requests.post(OVERPASS, data={"data": query}, headers=HEADERS, timeout=200)
    r.raise_for_status()
    return r.json().get("elements", [])


def delhi_schools() -> tuple[list[dict], str]:
    """Returns the schools and which query produced them ("area" or "bbox")."""
    elements, used = _run(BY_AREA), "area"
    if not elements:
        elements, used = _run(BY_BBOX), "bbox"
    schools = []
    for el in elements:
        point = el if el["type"] == "node" else el.get("center")
        if not point:
            continue
        tags = el.get("tags", {})
        schools.append(
            {
                "osm_id": f"{el['type']}/{el['id']}",
                "name": tags.get("name") or tags.get("name:en") or "",
                "lat": point["lat"],
                "lon": point["lon"],
            }
        )
    return schools, used
```

### `tests/test_hook.py` (Verbatim)

The test builds synthetic archive files stamped at the end of each hour and checks they land in the right IST
hour bin, that low-coverage stations drop out, and that the hook numbers come out right.

```python
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
```

**Check:** `uv run pytest -q` → `2 passed`; `uv run ruff check` → clean. Commit: `core: archive reader, hook stat, chart, API clients`.

## T3: Find Delhi's PM2.5 monitors → `data/stations.csv`

`scripts/find_stations.py`:

- Call `openaq_api.delhi_pm25_monitors()`.
- Write `data/stations.csv`. This file is small, so commit it.
- Print the total count and a breakdown by provider and owner.
- Print how many stations have `last_utc` on or after 2026-09-01 (still reporting).

Run: `uv run --env-file .env scripts/find_stations.py`

**Check:** expect a few dozen monitors. If there are fewer than 15, stop and show me the provider breakdown.
Commit: `data: list Delhi PM2.5 monitors`.

## T4: Confirm the timestamp convention (do not skip)

The OpenAQ docs don't say whether a reading is stamped at the start or the end of its hour. That decides
whether the hook stat's "8 AM" really is 8 AM.

`scripts/check_timestamps.py`:

1. Pick the still-reporting station with the longest history. Pick one day, 2025-11-12.
2. Read that day's archive file(s) with `archive.list_month_keys` + `fetch` + `read_pm25`.
3. Fetch the same PM2.5 sensor's raw values for that day from the API with the OpenAQ SDK:
   `OpenAQ(api_key=...).measurements.list(sensors_id=<pm25_sensor_id>, data="measurements", datetime_from=..., datetime_to=..., limit=1000)`.
4. Match rows by value (within 0.01). Each API result has a `period` with a start time and an `interval`, so its
   end time is start + interval.
5. Print a table of archive `datetime`, API period start and API period end.
6. Print the verdict: `ARCHIVE STAMPS = END` or `ARCHIVE STAMPS = START`. Base it on at least 10 matched rows.

Then:

- Set `STAMPED_AT_END` (True/False) in `scripts/config.py`. Every later script passes it to `archive.to_hourly`.
- Log the verdict in `docs/DECISIONS.md`.

If the SDK's attribute names differ from the above, inspect one result and adapt. Don't guess the verdict.
Commit: `data: confirm OpenAQ timestamp convention`.

## T5: Pull the archive → `data/processed/pm25_hourly.parquet` and `outputs/coverage.csv`

`scripts/pull_archive.py`:

- Months: October to January of four winters, 2022–23, 2023–24, 2024–25 and 2025–26. That is
  `(2022,10) … (2023,1)`, `(2023,10) … (2024,1)`, `(2024,10) … (2025,1)` and `(2025,10) … (2026,1)`.
- For each station × month:
  - Run `list_month_keys`.
  - Download with `fetch` in a `ThreadPoolExecutor(max_workers=16)` into `data/raw/`.
  - Run `read_pm25`, then `to_hourly(…, stamped_at_end=STAMPED_AT_END)`.
- Keep only the hourly frames in memory, never the raw rows. Write a single parquet file at the end.
- Coverage: for each station × winter, divide the hours with data by the hours in that winter's Oct–Jan.
  Write `outputs/coverage.csv`.
- Print:
  - total hourly rows, station count and date range
  - per winter, the number of stations with at least 60% coverage
  - the five stations with the worst coverage
- Re-runs reuse cached files.

**Check:** the printed summary. Commit `outputs/coverage.csv` with the script, not the data.
Commit: `data: pull four winters of hourly PM2.5`.

## T6: Hook stat → `outputs/hook_stat.json`, `outputs/hook_chart.png`, `outputs/hour_profile_2025.csv`

`scripts/hook_stat.py`:

- For Nov–Dec 2025 (`"2025-11-01"` to `"2026-01-01"`) and, as a cross-check, Nov–Dec 2024:
  `weekday_profile(hourly, start, end)` → `hook_numbers(profile)`.
- Write both periods, with their station counts, to `outputs/hook_stat.json`.
- Draw the chart for 2025 only: `hook_chart.draw(profile, nums, n_stations, "Nov–Dec 2025", Path("outputs/hook_chart.png"))`.
- Write the 2025 profile as `outputs/hour_profile_2025.csv` (columns `hour_ist,pm25`). The dashboard uses it later.

**Check:**

- Print both periods' numbers.
- Open the PNG and confirm no label collides with the line or another label.
- If `cut_pct` is 5 or less in either period, stop and show me. The story changes if the assembly hour isn't
  clearly worse.

Commit: `analysis: hook stat and chart`.

## T7: Schools → `data/schools.csv` and `data/pilot_schools.csv`

`scripts/schools.py`:

- Run `overpass.delhi_schools()`. Write `data/schools.csv`, then print the count and which query ran
  (`area` or `bbox`).
- Pilots:
  1. Keep stations with at least 60% coverage in 2025–26.
  2. For each school, compute the haversine distance to the nearest such station.
  3. Keep schools within 2 km of one.
  4. Prefer names matching `sarvodaya|govt|government|kendriya` (case-insensitive).
  5. Pick five, each nearest a different station, spread across the city: start with any one, then repeatedly
     add the school farthest from those already picked.
- Write `data/pilot_schools.csv` with columns school name, osm_id, lat, lon, station id, station name and
  distance in km.

**Check:** the five pilot rows are printed. Commit both CSVs.
Commit: `data: Delhi schools and five pilots`.

## T8: AWS skeleton (ask me before deploying)

Prerequisites:

- `aws --version` must show v2.
- `sam --version` must work. If SAM is missing, run `uv tool install aws-sam-cli`.
- `aws sts get-caller-identity --profile clearhour` must succeed.

### `template.yaml` (Verbatim)

```yaml
AWSTemplateFormatVersion: "2010-09-09"
Transform: AWS::Serverless-2016-10-31
Description: ClearHour - Phase 1 skeleton (one table, one scheduled Lambda)

Globals:
  Function:
    Runtime: python3.13
    Architectures: [x86_64]
    MemorySize: 256
    Timeout: 30
    Environment:
      Variables:
        TABLE_NAME: !Ref ClearHourTable

Resources:
  ClearHourTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      AttributeDefinitions:
        - AttributeName: pk
          AttributeType: S
        - AttributeName: sk
          AttributeType: S
      KeySchema:
        - AttributeName: pk
          KeyType: HASH
        - AttributeName: sk
          KeyType: RANGE
      TimeToLiveSpecification:
        AttributeName: ttl
        Enabled: true

  HeartbeatFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: functions/heartbeat/
      Handler: app.handler
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref ClearHourTable
      Events:
        Hourly:
          Type: ScheduleV2
          Properties:
            ScheduleExpression: rate(1 hour)

Outputs:
  TableName:
    Value: !Ref ClearHourTable
  HeartbeatFunctionName:
    Value: !Ref HeartbeatFunction
```

### `functions/heartbeat/app.py` (Verbatim)

```python
import os
import time
from datetime import UTC, datetime

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def handler(event, context):
    now = datetime.now(UTC).isoformat(timespec="seconds")
    TABLE.put_item(Item={"pk": "heartbeat", "sk": now, "ttl": int(time.time()) + 7 * 86400})
    return {"ok": True, "at": now}
```

```bash
uv run cfn-lint template.yaml
sam build
sam deploy --guided --profile clearhour   # stack: clearhour · region: ap-south-1 · confirm changes: Y
                                          # allow IAM role creation: Y · save to samconfig.toml: Y
sam remote invoke HeartbeatFunction --stack-name clearhour --profile clearhour
aws dynamodb scan --table-name <TableName from the stack outputs> --max-items 3 --profile clearhour
```

**Check:** the invoke returns `"ok": true`, and the scan shows a `heartbeat` item. Commit `template.yaml`,
`functions/` and `samconfig.toml`.
Commit: `infra: SAM skeleton with heartbeat Lambda and table`.

---

## Appendix A: Manual tasks for Shreyash (in parallel, not Claude Code)

**A1. OpenAQ key.** Sign up at explore.openaq.org, copy the key from settings into `.env` as `OPENAQ_API_KEY`.

**A2. AWS.**

- Create CLI credentials, then run `aws configure --profile clearhour` with region `ap-south-1`.
- In Billing, add a budget alert at a low amount.

**A3. WhatsApp sender.**

- Use a spare SIM that is **not** on WhatsApp. A number active on WhatsApp can't be registered until its
  WhatsApp account is deleted.
- In the AWS End User Messaging Social console (`ap-south-1`), choose Add WhatsApp phone number, then Launch
  Facebook portal.
- Create the business profile, with display name "ClearHour". Verify the number by SMS, then set the two-step PIN.
- Turn on the message and event destination. Point it to a new SNS topic, `clearhour-whatsapp-events`.
  Replies reach us through it on Friday.
- If the number is still not Active by Thursday evening, switch to Plan B: the test number Meta generates in
  its developer Get Started flow, called through the Cloud API from Lambda.

**A4. Alert template.**

- Name: `clearhour_daily_alert`. Category: Utility.
- Submit it in two languages, English (`en`) and Hindi (`hi`). Each body starts and ends with plain text,
  never a variable.
- English body:

  > ClearHour air update for {{1}} on {{2}}: {{3}} Cleanest hour for outdoor activity: {{4}}. Reply 1 once you have moved outdoor activities.

  Samples: {{1}} `Sarvodaya Vidyalaya, Rohini Sector 8` · {{2}} `Thu 13 Nov` · {{3}} `Hold assembly indoors.` · {{4}} `1:00–2:00 PM`

- Hindi body:

  > ClearHour वायु सूचना – {{1}}, {{2}}: {{3}} बाहरी गतिविधियों के लिए सबसे साफ़ समय: {{4}}। गतिविधियाँ बदलने के बाद 1 लिखकर भेजें।

  Samples: {{1}} `सर्वोदय विद्यालय, रोहिणी सेक्टर 8` · {{2}} `गुरु 13 नवंबर` · {{3}} `प्रार्थना सभा अंदर करें।` · {{4}} `दोपहर 1:00–2:00`

- How the variables cover each case:
  - Fine-air day: {{3}} is "Air is fine for outdoor activity today." and {{4}} is "any time".
  - No-safe-window day: {{3}} is the no-window sentence and {{4}} is "none today".

## Appendix B: Report back to the architect

Paste this, filled in, into the Claude chat when Phase 1 is done or stuck:

```
PHASE 1 REPORT
Stations: <n> PM2.5 monitors (<n> still reporting); providers/owners: <breakdown>
Timestamps: archive stamps = <START|END>, <n> rows matched
Archive: <rows> hourly rows, <n> stations, <first> to <last>
Coverage ≥60%: 2022–23 <n> · 2023–24 <n> · 2024–25 <n> · 2025–26 <n>
Hook 2025: assembly <x> µg/m³ · cleanest <hh:00–hh:00> <y> µg/m³ · cut <z>%
Hook 2024: assembly <x> · cleanest <hour> <y> · cut <z>%
Schools: <n> via <area|bbox>; pilots: <name, station, km> ×5
AWS: stack clearhour <deployed?>; heartbeat items <n>
WhatsApp: number <status>; templates en <status>, hi <status>
Failures (verbatim): <...>
DECISIONS.md: <new lines>
```

## T6 (revised 8 Oct, 01:10): Hook stat on 2025–26 data
Nov–Dec 2024 has no station data, so the 2024 cross-check is replaced. Write everything to outputs/hook_stat.json.
1. Nov–Dec 2025 profile, hook numbers and chart, as in the original T6.
2. Day-level consistency, Nov–Dec 2025, weekdays only, station-days with all six school hours present:
   - cut_day = (PM2.5 at 08:00 − lowest of 08:00–13:00) / PM2.5 at 08:00
   - report the station-day count, the median cut_day, and the share of station-days with cut_day ≥ 20%
   - report the share of station-days whose cleanest school hour is 12:00 or 13:00
3. Multi-year shape at the US Embassy monitor (8118), Nov–Dec of 2022–2025, using 8118's own timestamp
   verdict in to_hourly. Per year: weekday_profile for that station alone, then hook_numbers. If 8118's
   verdict wasn't clear, skip this item and log why.
   Amended 8 Oct, 01:56: for 2023 use 4 Nov to 31 Dec, because 1–3 Nov carries two interleaved series at
   8118. Record that in hook_stat.json. (pm25_hourly.parquet already bins 8118 with its START verdict.)
Check: print all of it. If the median cut_day is 5% or less, stop and show me.
