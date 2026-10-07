"""DynamoDB access for the single ClearHour table (keys as in CLAUDE.md)."""

from __future__ import annotations

import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

_TABLE = None


def table():
    global _TABLE
    if _TABLE is None:
        _TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _TABLE


def _scan(**kwargs) -> list[dict]:
    items, start = [], None
    while True:
        resp = table().scan(**kwargs, **({"ExclusiveStartKey": start} if start else {}))
        items += resp["Items"]
        start = resp.get("LastEvaluatedKey")
        if not start:
            return items


def put_obs(location_id: int, hour_utc: str, pm25: float, n: int, ttl_days: int = 60) -> None:
    table().put_item(
        Item={
            "pk": f"STATION#{location_id}",
            "sk": f"OBS#{hour_utc}",
            "pm25": Decimal(str(round(pm25, 2))),
            "n": n,
            "ttl": int(time.time()) + ttl_days * 86400,
        }
    )


def get_obs(location_id: int, start_utc: str, end_utc: str) -> list[dict]:
    """Hourly readings with hour_utc between the two ISO strings (inclusive)."""
    items, start = [], None
    cond = Key("pk").eq(f"STATION#{location_id}") & Key("sk").between(f"OBS#{start_utc}", f"OBS#{end_utc}")
    while True:
        resp = table().query(KeyConditionExpression=cond, **({"ExclusiveStartKey": start} if start else {}))
        items += resp["Items"]
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
    return [{"hour_utc": it["sk"][4:], "pm25": float(it["pm25"])} for it in items]


def profiles() -> list[dict]:
    """Subscribed schools: SCHOOL#<id> / PROFILE with name, phone, lang."""
    return _scan(FilterExpression=Attr("sk").eq("PROFILE"))


def profile_by_phone(phone: str) -> dict | None:
    hits = _scan(FilterExpression=Attr("sk").eq("PROFILE") & Attr("phone").eq(phone))
    return hits[0] if hits else None


def alert_key(school_id: str, day: str) -> dict:
    return {"pk": f"SCHOOL#{school_id}", "sk": f"ALERT#{day}"}


def put_alert_if_new(school_id: str, day: str, attrs: dict, *, overwrite: bool = False) -> bool:
    """Creates the day's alert in status 'pending'. False if one already exists (unless overwrite)."""
    item = {**alert_key(school_id, day), **attrs, "status": "pending", "ttl": int(time.time()) + 90 * 86400}
    try:
        if overwrite:
            table().put_item(Item=item)
        else:
            table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def get_alert(school_id: str, day: str) -> dict | None:
    return table().get_item(Key=alert_key(school_id, day)).get("Item")


def move_alert(school_id: str, day: str, from_status: str, to_status: str, **attrs) -> bool:
    """Atomically moves an alert between statuses; False if it wasn't in from_status."""
    names = {"#s": "status", **{f"#{k}": k for k in attrs}}
    values = {":from": from_status, ":to": to_status, **{f":{k}": v for k, v in attrs.items()}}
    sets = ", ".join(["#s = :to", *[f"#{k} = :{k}" for k in attrs]])
    try:
        table().update_item(
            Key=alert_key(school_id, day),
            UpdateExpression=f"SET {sets}",
            ConditionExpression="#s = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def mark_acted(school_id: str, day: str, at: str) -> bool:
    try:
        table().update_item(
            Key=alert_key(school_id, day),
            UpdateExpression="SET acted = :t, acted_at = :at",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":t": True, ":at": at},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
