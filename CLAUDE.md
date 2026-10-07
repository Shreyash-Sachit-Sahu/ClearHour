# ClearHour

Solo entry for Environmental Hacks (Bharat Builds Tour, WeMakeDevs × AWS), Air track, Oct 8–11 2026.
Every school morning ClearHour forecasts PM2.5 hour by hour at each Delhi school and sends the principal one
WhatsApp message by 06:30 IST naming the cleanest hour for assembly and PE (the "Clear Hour"). On days with no
safe hour it says so. A dashboard shows which schools are at risk and which have acted.

## Hackathon rules that shape the code

- Everything in this repo is written during the event. Commit small and often with real messages; the git
  history must match the event dates. No amend, rebase, squash or force-push. Never copy code from other repos.
- The demo video must show AWS doing real work, so AWS is the backbone, not just hosting.
- Working beats polished: one feature that runs end to end beats five that almost do.
- The README must list the AI tools used (Claude for planning and briefs, Claude Code for implementation).
- The repo is public: no secrets, keys or phone numbers in git, ever.

## How we work

- Phase briefs from the architect (Claude chat) live in `docs/briefs/`. Implement the current brief only.
- Code marked **Verbatim** in a brief was tested before handover: create it exactly. If it fails on real data,
  make the smallest fix and log it in `docs/DECISIONS.md`.
- After each task: run its check, commit, then stop and show me the output before the next task.
- Anything a brief leaves open: pick the simplest option that meets the day's gate and log it in
  `docs/DECISIONS.md` (one line: date, decision, why).
- At the end of a phase, fill in the brief's report block so I can paste it back to the architect.

## Environment

- Windows 11 + WSL2 (Ubuntu). Run everything inside WSL2. Keep the repo in the Linux filesystem
  (`~/code/clearhour`), not under `/mnt/c`.
- Docker Desktop with WSL integration (needed later for the LightGBM Lambda container image).
- 16 GB RAM: process the data archive one month at a time; never load all raw files at once.
- AWS region `ap-south-1` (Mumbai) for every resource. CLI profile name: `clearhour`.

## Stack and versions

- Python 3.13 everywhere, matching the Lambda `python3.13` runtime. Package manager: uv, lockfile committed.
- Pins: pandas 3.0.x, numpy 2.x, pyarrow, matplotlib 3.11.x, lightgbm 4.7.x, scikit-learn 1.9.x, requests,
  boto3, openaq (the OpenAQ SDK). Dev: pytest, ruff, cfn-lint.
- pandas 3: Copy-on-Write is the default, so never rely on chained assignment.
- Infrastructure: AWS SAM, `template.yaml` at the repo root. No CDK, no Terraform.
- Lambda architecture: x86_64 (the dev machine is x86, so container builds need no emulation).
- Lambda packaging: zip functions use CodeUri `src/` and may import only the standard library, boto3 and the
  dependency-free modules (`constants`, `decide`, `store`, `whatsapp`). Anything that needs pandas or LightGBM
  runs in the Forecast container image.
- Dashboard (Saturday): static site, Vite + MapLibre, Node 24, on Amplify Hosting.

## Repo layout

```
CLAUDE.md
template.yaml            SAM template (all AWS resources)
samconfig.toml           written by `sam deploy --guided`
src/clearhour/           library code shared by scripts and Lambdas
functions/<name>/        one folder per Lambda handler
scripts/                 data jobs, run as `uv run --env-file .env scripts/<name>.py`
tests/                   pytest, fast (under 10 s)
data/                    raw/ and processed/ are gitignored; small reference CSVs are committed
outputs/                 charts, JSON and CSV for the README and video (committed)
web/                     dashboard (Saturday)
docs/briefs/             phase briefs (input)
docs/DECISIONS.md        one line per shortcut or deviation
```

## Domain rules (don't change without asking)

- Store timestamps in UTC; convert to IST (`Asia/Kolkata`) only at the edges. Hour bins are hour-beginning.
- School hours default to 08:00–14:00 IST (bins 08 to 13), with a per-school override. Assembly = 08:00 bin.
- The daily run starts at 05:30 IST and messages go out by 06:30 IST.
- PM2.5 bands (CPCB National AQI, µg/m³, defined for 24-hour means): 0–30 good, 31–60 satisfactory,
  61–90 moderate, 91–120 poor, 121–250 very poor, above 250 severe. We apply them to hourly values; the
  README says so.
- Decision rule per school per day:
  1. Every school hour ≤ 90 → "Air is fine for outdoor activity today."
  2. Every school hour > 250 → "No safe window today. Keep assembly, PE and recess indoors."
  3. Otherwise the Clear Hour is the cleanest school hour; also move assembly indoors if its hour is > 120.
- Data: OpenAQ holds no Delhi CPCB data from about 2020 to January 2025. Station data resumes in
  February 2025; the US Embassy monitor (8118) is the only continuous long record.
- Model: one LightGBM regressor across stations, station as a categorical feature. Evaluate walk-forward on
  the 2025–26 winter: for each week from 10 Nov 2025 to 30 Jan 2026, train on everything before that week and
  predict its school mornings. Never shuffle hours. Baselines: persistence and raw CAMS. The deployed model
  trains on all data to date, and October 2026 mornings are the live check.
- Headline metric: share of test mornings where the chosen hour is among the two cleanest actual school hours.
- Report concentration differences only. Never claim health outcomes.

## Data sources and their quirks

- OpenAQ archive: `s3://openaq-data-archive/records/csv.gz/locationid={id}/year={yyyy}/month={mm}/`, read
  anonymously (boto3 with `UNSIGNED`), one gzipped CSV per location-day, landing about 72 h after the day ends.
  List the prefix instead of building file names. Column names vary (`sensors_id`/`sensor_id`,
  `units`/`unit`): normalise on load. Files can be patched later.
- OpenAQ API v3: `https://api.openaq.org/v3`, key in header `X-API-Key` from env `OPENAQ_API_KEY`.
  `bbox` is min lon, min lat, max lon, max lat (4 decimals max); `monitor=true` keeps reference monitors.
- Open-Meteo air quality: `pm2_5` from the CAMS global domain (~45 km, 3-hourly, 5 days ahead, archived from
  Aug 2022). No key for non-commercial use. The README must credit CAMS and Open-Meteo.
- OpenStreetMap via the Overpass API for school locations; send a descriptive User-Agent.
- Credit OpenAQ and the station owners (CPCB, DPCC, IMD) in the README; OpenAQ licences vary by provider.

## DynamoDB (single table, `pk` + `sk`, TTL attribute `ttl`)

| Item | pk | sk |
| --- | --- | --- |
| School profile | `SCHOOL#<id>` | `PROFILE` |
| Daily decision and alert (idempotency key) | `SCHOOL#<id>` | `ALERT#<YYYY-MM-DD>` |
| Principal reply | `SCHOOL#<id>` | `REPLY#<iso timestamp>` |
| Station reading | `STATION#<id>` | `OBS#<hour_utc iso>` |
| Heartbeat | `heartbeat` | `<iso timestamp>` |

Sending is idempotent: write `ALERT#<date>` with a condition that it doesn't exist before sending, so a re-run
never double-sends.

## Secrets

- `.env` (gitignored) holds `OPENAQ_API_KEY`; `.env.example` is committed with empty values. Never print keys.
- Lambda secrets come in as SAM parameters with `NoEcho: true`, never as literals in the template.
- Principal phone numbers live only in DynamoDB, never in the repo.

## Conventions

- Type hints, small functions, `ruff check` and `ruff format` clean (line length 120).
- Every script ends with a one-screen summary: counts, date range, gaps, files written.
- Scripts are re-runnable: cache downloads, skip work already done.
- Commit messages are imperative and scoped, e.g. `data: pull OpenAQ archive for Delhi monitors`.

## Commands

```bash
uv sync
uv run pytest -q
uv run ruff check --fix && uv run ruff format
uv run --env-file .env scripts/<name>.py
uv run cfn-lint template.yaml
./scripts/deploy.sh      # never `sam deploy --guided` again: it saves parameter values, including the API key, into samconfig.toml
sam remote invoke <Function> --stack-name clearhour
```
