"""Send Lambda (one school per call, from the Step Functions Map): pending alert -> WhatsApp template.

The alert moves pending -> sending -> sent with conditional writes, so a retried or duplicated call never
sends twice. A failed send goes back to pending and re-raises, so Step Functions can retry it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from clearhour import store, whatsapp


def handler(event, context):
    school_id, day = event["school_id"], event["day"]
    alert = store.get_alert(school_id, day)
    if not alert or alert.get("status") != "pending":
        return {"school_id": school_id, "status": "skipped", "reason": alert.get("status") if alert else "missing"}
    if not store.move_alert(school_id, day, "pending", "sending"):
        return {"school_id": school_id, "status": "skipped", "reason": "claimed by another run"}
    profile = store.table().get_item(Key={"pk": f"SCHOOL#{school_id}", "sk": "PROFILE"})["Item"]
    lang = profile.get("lang", "en")
    payload = whatsapp.template_payload(
        profile["phone"],
        list(alert["params"]),
        name=os.environ.get("WA_TEMPLATE_NAME", "clearhour_daily_alert"),
        lang=os.environ.get(f"WA_TEMPLATE_LANG_{lang.upper()}", lang),
    )
    try:
        message_id = whatsapp.send(payload)
    except Exception as e:
        store.move_alert(school_id, day, "sending", "pending", last_error=str(e)[:500])
        raise
    store.move_alert(
        school_id,
        day,
        "sending",
        "sent",
        message_id=message_id,
        sent_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return {"school_id": school_id, "status": "sent", "message_id": message_id}
