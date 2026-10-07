"""T3: Delhi's reference PM2.5 monitors from the OpenAQ API -> data/stations.csv."""

from pathlib import Path

import pandas as pd

from clearhour.openaq_api import delhi_pm25_monitors

OUT = Path("data/stations.csv")
STILL_REPORTING = pd.Timestamp("2026-09-01", tz="UTC")


def main() -> None:
    stations = pd.DataFrame(delhi_pm25_monitors()).sort_values("location_id")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    stations.to_csv(OUT, index=False)

    first = pd.to_datetime(stations["first_utc"], utc=True)
    last = pd.to_datetime(stations["last_utc"], utc=True)
    print(f"{len(stations)} Delhi PM2.5 reference monitors, by provider and owner:")
    print(stations.groupby(["provider", "owner"], dropna=False).size().to_string())
    print(f"still reporting (last reading on or after {STILL_REPORTING:%Y-%m-%d}): {(last >= STILL_REPORTING).sum()}")
    print(f"history: first reading {first.min():%Y-%m-%d}, last {last.max():%Y-%m-%d}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
