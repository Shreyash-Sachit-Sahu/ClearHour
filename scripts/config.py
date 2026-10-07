"""Settings shared by the data scripts."""

# Archive timestamp convention per provider (the provider column of data/stations.csv), each from a
# scripts/check_timestamps.py run. True = a reading is stamped at the end of the period it covers.
# The pull skips stations whose provider is missing here: check one of its stations first, never default.
STAMPED_AT_END = {
    "CPCB": True,  # station 235, 2025-11-12, 15-min: 84/84 rows
    "caaqm": True,  # station 7044, 2022-10-12, hourly: 16/16 rows
    "AirNow": False,  # station 8118 (US Embassy), 2023-11-14, hourly: 23/23 rows
}
