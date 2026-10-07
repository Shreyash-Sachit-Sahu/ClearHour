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
        (ASSEMBLY_HOUR + 0.5, a_val),
        xytext=(-14, 10),
        textcoords="offset points",
        ha="right",
        va="bottom",
        fontsize=9,
        color=INK,
    )
    ax.annotate(
        f"Cleanest school hour, {_clock(best)}–{_clock(best + 1)}\n{b_val:.0f} µg/m³",
        (best + 0.5, b_val),
        xytext=(14, 12),
        textcoords="offset points",
        ha="left",
        va="bottom",
        fontsize=9,
        color=INK,
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

    fig.text(
        0.04,
        0.95,
        f"Moving outdoor time from {_clock(ASSEMBLY_HOUR)} to {_clock(best)} cuts PM2.5 by {nums['cut_pct']:.0f}%",
        fontsize=13,
        fontweight="semibold",
        color=INK,
        va="top",
    )
    fig.text(
        0.04,
        0.885,
        f"Mean PM2.5 by hour of day (µg/m³) · {n_stations} Delhi monitors · school days, {period}",
        fontsize=9,
        color=INK_2,
        va="top",
    )
    fig.text(
        0.04,
        0.03,
        "Source: Delhi reference monitors via OpenAQ (AWS Open Data). Hourly means, weekdays only.",
        fontsize=7.5,
        color=MUTED,
    )
    fig.subplots_adjust(left=0.07, right=0.97, top=0.80, bottom=0.14)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
