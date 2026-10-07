# Decisions

- 2026-10-08: Verbatim means the logic, not the layout: ruff format is allowed on handed-over files (hook.py and hook_chart.py reformatted, layout only). docs/ is excluded from ruff so briefs are never reformatted.
- 2026-10-08: OpenAQ archive stamps each reading at the END of its period (T4: station 235 Anand Vihar, 2025-11-12, 15-min periods; 84/84 rows equal the API period ending at the stamp, 56/76 the one starting there). STAMPED_AT_END = True.
- 2026-10-08: T4 lines rows up by timestamp and compares values instead of matching unique values: CPCB repeats each hourly value across its four 15-min slots, so only 1 of 84 values was unique.
- 2026-10-08: to_hourly floors on the IST clock, not UTC. Archive data is 15-minute, and with UTC flooring the "08" bin held 08:30-09:30 IST because IST is UTC+5:30. hour_utc is still stored in UTC (now at :30); a 15-minute test covers it.
