"""WhatsApp payloads, sending (AWS End User Messaging Social, Meta Cloud API, or dry run) and inbound parsing."""

from __future__ import annotations

import json
import os
import urllib.request

import boto3

DEFAULT_META_API_VERSION = "v20.0"


def template_payload(to: str, params: list[str], *, name: str, lang: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}],
        },
    }


def text_payload(to: str, body: str) -> dict:
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}


def send(payload: dict) -> str:
    """Send one message; returns the provider's message id. WA_MODE picks the route."""
    mode = os.environ.get("WA_MODE", "dry_run")
    version = os.environ.get("META_API_VERSION", DEFAULT_META_API_VERSION)
    if mode == "eum":
        resp = boto3.client("socialmessaging").send_whatsapp_message(
            originationPhoneNumberId=os.environ["WA_PHONE_NUMBER_ID"],
            message=json.dumps(payload).encode(),
            metaApiVersion=version,
        )
        return resp["messageId"]
    if mode == "meta":  # Plan B: Meta's Cloud API directly, e.g. with Meta's test number
        req = urllib.request.Request(
            f"https://graph.facebook.com/{version}/{os.environ['META_PHONE_NUMBER_ID']}/messages",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {os.environ['META_ACCESS_TOKEN']}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)["messages"][0]["id"]
    if mode == "dry_run":
        print(json.dumps({"dry_run_payload": payload}, ensure_ascii=False))
        return "dry-run"
    raise ValueError(f"unknown WA_MODE {mode!r}")


def parse_sns(event: dict) -> list[dict]:
    """Text messages from End User Messaging Social's SNS events: sender, text, message id, timestamp.

    The SNS message's whatsAppWebhookEntry is Meta's webhook entry as a JSON string.
    """
    out = []
    for record in event.get("Records", []):
        body = json.loads(record["Sns"]["Message"])
        entry = json.loads(body["whatsAppWebhookEntry"])
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                if msg.get("type") == "text":
                    out.append(
                        {
                            "from": msg["from"],
                            "text": msg["text"]["body"].strip(),
                            "id": msg["id"],
                            "timestamp": msg.get("timestamp"),
                        }
                    )
    return out
