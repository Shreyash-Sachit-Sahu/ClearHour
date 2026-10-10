"""Inbound Lambda (SNS from End User Messaging Social): a principal's reply.

"1" (or "१", "done") marks the alert sent in the last 24 hours, a replay included, as acted on; anything else
gets a short help reply. Replies are free-form text, allowed because the principal has just messaged us
(WhatsApp's 24-hour window).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clearhour import site, store, whatsapp

YES = {"1", "1.", "१", "done", "ok 1", "हो गया"}

REPLIES = {
    "en": {
        "thanks": "Thanks, noted for {name} today.",
        "no_alert": "Thanks! There is no ClearHour alert for today yet.",
        "help": (
            "ClearHour sends one air alert each school morning by 6:30. Reply 1 once you have moved outdoor activity."
        ),
    },
    "hi": {
        "thanks": "धन्यवाद, आज {name} के लिए दर्ज कर लिया गया।",
        "no_alert": "धन्यवाद! आज के लिए अभी कोई ClearHour सूचना नहीं है।",
        "help": "ClearHour हर स्कूल सुबह 6:30 तक हवा की एक सूचना भेजता है। गतिविधियाँ बदलने के बाद 1 लिखकर भेजें।",
    },
}
UNKNOWN = "This number isn't registered with ClearHour yet."


def answered_day(school_id: str, now: datetime) -> str | None:
    """The alert a "1" answers: the one sent most recently, within the last 24 hours."""
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    sent = [a for a in site.recent_alerts(school_id) if a.get("sent_at", "") >= since]
    return max(sent, key=lambda a: a["sent_at"])["sk"].split("#", 1)[1] if sent else None


def handler(event, context):
    handled = 0
    for msg in whatsapp.parse_sns(event):
        profile = store.profile_by_phone(msg["from"])
        if not profile:
            whatsapp.send(whatsapp.text_payload(msg["from"], UNKNOWN))
            continue
        school_id = profile["pk"].split("#", 1)[1]
        text = REPLIES.get(profile.get("lang", "en"), REPLIES["en"])
        if msg["text"].lower() in YES:
            now = datetime.now(UTC)
            day = answered_day(school_id, now)
            acted = day is not None and store.mark_acted(school_id, day, now.isoformat(timespec="seconds"))
            body = text["thanks"].format(name=profile["name"]) if acted else text["no_alert"]
            if acted:
                site.publish_alerts_quietly()
        else:
            body = text["help"]
        whatsapp.send(whatsapp.text_payload(msg["from"], body))
        handled += 1
    return {"handled": handled}
