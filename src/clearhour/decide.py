"""The decision rule and the four template variables of the WhatsApp alert (English and Hindi)."""

from __future__ import annotations

from datetime import date

from clearhour.constants import ASSEMBLY_HOUR, TARGET_HOURS

FINE_MAX = 90  # every school hour at or below: air is fine
SEVERE_MIN = 250  # every school hour above: no safe window
ASSEMBLY_INDOOR_MIN = 120  # assembly hour above: hold assembly indoors

EN_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EN_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
HI_DOW = ["सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि", "रवि"]
HI_MON = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]

TEXT = {
    "en": {
        "fine": "Air is fine for outdoor activity today.",
        "no_window": "No safe window today. Keep assembly, PE and recess indoors.",
        "assembly_in": "Hold assembly indoors.",
        "assembly_ok": "Assembly can stay outdoors.",
        "any_time": "any time",
        "none_today": "none today",
        "replay": " (replay)",
    },
    "hi": {
        "fine": "आज बाहरी गतिविधियों के लिए हवा ठीक है।",
        "no_window": "आज कोई सुरक्षित समय नहीं है। प्रार्थना सभा, पीटी और खेल अंदर ही कराएँ।",
        "assembly_in": "प्रार्थना सभा अंदर करें।",
        "assembly_ok": "प्रार्थना सभा बाहर हो सकती है।",
        "any_time": "किसी भी समय",
        "none_today": "आज कोई नहीं",
        "replay": " (रीप्ले)",
    },
}


def decide(hourly: dict[int, float]) -> dict:
    """hourly: predicted PM2.5 per school hour (08..13). Returns the kind of day and the Clear Hour."""
    vals = {h: float(hourly[h]) for h in TARGET_HOURS}
    if all(v <= FINE_MAX for v in vals.values()):
        return {"kind": "fine", "clear_hour": None, "assembly_indoors": False}
    if all(v > SEVERE_MIN for v in vals.values()):
        return {"kind": "no_window", "clear_hour": None, "assembly_indoors": True}
    clear = min(vals, key=vals.get)
    return {"kind": "clear_hour", "clear_hour": clear, "assembly_indoors": vals[ASSEMBLY_HOUR] > ASSEMBLY_INDOOR_MIN}


def _clock(h: int) -> tuple[int, str]:
    return (h % 12) or 12, "AM" if h % 24 < 12 else "PM"


def slot_label(hour: int, lang: str) -> str:
    """'1:00–2:00 PM' / '11:00 AM–12:00 PM' in English; 'दोपहर 1:00–2:00' in Hindi."""
    (a, ma), (b, mb) = _clock(hour), _clock(hour + 1)
    if lang == "hi":
        period = "सुबह" if hour < 12 else "दोपहर" if hour < 16 else "शाम"
        return f"{period} {a}:00–{b}:00"
    return f"{a}:00–{b}:00 {ma}" if ma == mb else f"{a}:00 {ma}–{b}:00 {mb}"


def day_label(day: date, lang: str) -> str:
    if lang == "hi":
        return f"{HI_DOW[day.weekday()]} {day.day} {HI_MON[day.month - 1]}"
    return f"{EN_DOW[day.weekday()]} {day.day} {EN_MON[day.month - 1]}"


def message_params(decision: dict, school_name: str, day: date, lang: str = "en", replay: bool = False) -> list[str]:
    """The template's {{1}}..{{4}}: school, day, advice sentence, Clear Hour."""
    t = TEXT[lang]
    when = day_label(day, lang) + (t["replay"] if replay else "")
    if decision["kind"] == "fine":
        advice, slot = t["fine"], t["any_time"]
    elif decision["kind"] == "no_window":
        advice, slot = t["no_window"], t["none_today"]
    else:
        advice = t["assembly_in"] if decision["assembly_indoors"] else t["assembly_ok"]
        slot = slot_label(decision["clear_hour"], lang)
    return [school_name, when, advice, slot]
