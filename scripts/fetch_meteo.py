"""P2-T2: city-point weather and CAMS PM2.5 history from Open-Meteo -> data/processed/meteo.parquet."""

from pathlib import Path

import pandas as pd

from clearhour import meteo
from clearhour.constants import IST

OUT = Path("data/processed/meteo.parquet")
MONTHS = ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-10"]  # Oct 2026 runs up to yesterday


def main() -> None:
    yesterday = pd.Timestamp.now(tz=IST).tz_localize(None).normalize() - pd.Timedelta(days=1)
    frames = []
    for month in MONTHS:
        first = pd.Timestamp(f"{month}-01")
        last = min(first + pd.offsets.MonthEnd(0), yesterday)
        frames.append(meteo.fetch(str(first.date()), str(last.date()), live=False))
        print(f"  {first:%Y-%m-%d} to {last:%Y-%m-%d}: {len(frames[-1])} hours")
    met = pd.concat(frames)
    met = met[~met.index.duplicated()].sort_index()
    met.index.name = "hour_ist"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    met.to_parquet(OUT)

    print(f"\n{len(met):,} hours, {met.index.min()} to {met.index.max()}")
    print(f"index minutes (IST): {met.index.minute.value_counts().to_dict()}")
    print("share missing per column:")
    print(met.isna().mean().round(3).to_string())
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
