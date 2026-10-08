"""P2-T7 step 1: subscribe the pilot schools, one SCHOOL#<osm_id> / PROFILE item each (name, phone, lang).

Reads data/pilot_schools.csv and, from .env, PILOT_PHONES (comma-separated, country code plus digits, in the CSV's
order; fewer numbers than schools is fine) and PILOT_LANGS (e.g. hi,en). Run with --env-file .env.
Phone numbers live only in .env and DynamoDB: this script never prints one in full.
"""

import os
from pathlib import Path

import boto3
import pandas as pd

PILOTS = Path("data/pilot_schools.csv")
STACK = "clearhour"


def table_name() -> str:
    outputs = boto3.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "TableName")


def masked(phone: str) -> str:
    return "***" + phone[-4:]


def main() -> None:
    phones = [p.strip() for p in os.environ.get("PILOT_PHONES", "").split(",") if p.strip()]
    if not phones:
        raise SystemExit("PILOT_PHONES is empty in .env: add the pilot numbers there (country code plus digits)")
    langs = [x.strip() for x in os.environ.get("PILOT_LANGS", "").split(",") if x.strip()]
    pilots = pd.read_csv(PILOTS)
    table = boto3.resource("dynamodb").Table(table_name())
    for i, (p, phone) in enumerate(zip(pilots.itertuples(), phones, strict=False)):
        lang = langs[i] if i < len(langs) else "en"
        if not phone.isdigit() or lang not in ("en", "hi"):
            raise SystemExit(f"pilot {i + 1}: phone {masked(phone)} must be digits only, lang must be en or hi")
        item = {"pk": f"SCHOOL#{p.osm_id}", "sk": "PROFILE", "name": p.display_name, "phone": phone, "lang": lang}
        table.put_item(Item=item)
        print(f"PROFILE SCHOOL#{p.osm_id}: {p.display_name} · {masked(phone)} · {lang}")
    print(f"seeded {min(len(phones), len(pilots))} of {len(pilots)} pilot schools into {table.name}")


if __name__ == "__main__":
    main()
