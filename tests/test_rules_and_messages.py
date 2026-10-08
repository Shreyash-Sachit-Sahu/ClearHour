import json
from datetime import date

from clearhour import openaq_api
from clearhour.decide import decide, message_params, render, slot_label
from clearhour.handlers.ingest import hourly_means, lookback_hours
from clearhour.whatsapp import parse_sns, send, template_payload


def _hours(*vals):
    return dict(zip(range(8, 14), vals, strict=True))


def test_decision_rule_three_kinds():
    assert decide(_hours(60, 70, 80, 90, 85, 75))["kind"] == "fine"
    assert decide(_hours(400, 380, 350, 320, 300, 260))["kind"] == "no_window"
    d = decide(_hours(300, 260, 220, 180, 150, 160))
    assert d == {"kind": "clear_hour", "clear_hour": 12, "assembly_indoors": True, "limit_outdoor": True}
    assert decide(_hours(110, 100, 95, 92, 91, 140))["assembly_indoors"] is False


def test_latest_reading_makes_the_assembly_call_and_guards_fine():
    calm = _hours(100, 95, 90, 85, 80, 85)
    assert decide(calm)["assembly_indoors"] is False
    assert decide(calm, latest=150)["assembly_indoors"] is True  # the 04:00 reading overrules the 08:00 forecast
    assert decide(_hours(150, 140, 130, 100, 90, 95), latest=100)["assembly_indoors"] is False
    fine = _hours(60, 70, 80, 90, 85, 75)
    assert decide(fine, latest=80)["kind"] == "fine"
    held = decide(fine, latest=130)  # forecast says fine, but the air near the school is poor right now
    assert held == {"kind": "clear_hour", "clear_hour": 9, "assembly_indoors": True, "limit_outdoor": False}


def test_a_fixed_hour_replaces_the_forecast_pick_on_stale_mornings():
    hourly = _hours(300, 260, 220, 180, 150, 160)  # the forecast's cleanest is 12:00
    assert decide(hourly)["clear_hour"] == 12
    d = decide(hourly, fixed_hour=13)
    assert d["clear_hour"] == 13 and d["limit_outdoor"] is True
    assert decide(_hours(60, 70, 80, 90, 85, 75), fixed_hour=13)["kind"] == "fine"  # fine days name no hour


def test_keep_short_advice_when_even_the_clear_hour_is_very_poor():
    d = decide(_hours(300, 260, 220, 180, 150, 160))
    en = message_params(d, "KV RK Puram", date(2025, 11, 13), "en")
    assert en[2:] == ["Hold assembly indoors. Keep outdoor time short.", "12:00–1:00 PM"]
    hi = message_params(d, "केवी आरके पुरम", date(2025, 11, 13), "hi")
    assert hi[2] == "प्रार्थना सभा अंदर करें। बच्चों को बाहर कम समय ही रखें।"


def test_labels_and_params_in_both_languages():
    assert slot_label(13, "en") == "1:00–2:00 PM"
    assert slot_label(11, "en") == "11:00 AM–12:00 PM"
    assert slot_label(13, "hi") == "दोपहर 1:00–2:00"
    d = {"kind": "clear_hour", "clear_hour": 13, "assembly_indoors": True}
    en = message_params(d, "Sarvodaya Vidyalaya", date(2025, 11, 13), "en")
    assert en == ["Sarvodaya Vidyalaya", "Thu 13 Nov", "Hold assembly indoors.", "1:00–2:00 PM"]
    hi = message_params(d, "सर्वोदय विद्यालय", date(2025, 11, 13), "hi", replay=True)
    assert hi == ["सर्वोदय विद्यालय", "गुरु 13 नवंबर (रीप्ले)", "प्रार्थना सभा अंदर करें।", "दोपहर 1:00–2:00"]
    for p in en + hi:  # WhatsApp rejects template parameters with newlines or tabs
        assert "\n" not in p and "\t" not in p


def test_template_payload_shape():
    p = template_payload("919999999999", ["a", "b", "c", "d"], name="clearhour_daily_alert", lang="en")
    assert p["type"] == "template" and p["template"]["language"] == {"code": "en"}
    assert [x["text"] for x in p["template"]["components"][0]["parameters"]] == ["a", "b", "c", "d"]


def test_parse_sns_reads_text_messages_and_ignores_statuses():
    entry = {
        "id": "1",
        "changes": [
            {
                "value": {
                    "messages": [
                        {
                            "from": "919999999999",
                            "id": "wamid.X",
                            "timestamp": "1",
                            "type": "text",
                            "text": {"body": " 1 "},
                        }
                    ]
                }
            },
            {"value": {"statuses": [{"id": "wamid.Y", "status": "sent"}]}},
        ],
    }
    event = {"Records": [{"Sns": {"Message": json.dumps({"context": {}, "whatsAppWebhookEntry": json.dumps(entry)})}}]}
    assert parse_sns(event) == [{"from": "919999999999", "text": "1", "id": "wamid.X", "timestamp": "1"}]


def test_ingest_bins_fifteen_minute_periods_by_ist_clock_hour():
    def m(start_utc, v):
        return {"value": v, "period": {"datetimeFrom": {"utc": start_utc}}}

    # 08:00-09:00 IST is 02:30-03:30 UTC; a 08:45 IST start (03:15 UTC) belongs to the 08:00 hour
    res = [
        m("2025-11-13T02:30:00Z", 100),
        m("2025-11-13T02:45:00Z", 110),
        m("2025-11-13T03:15:00Z", 130),
        m("2025-11-13T03:30:00Z", 50),
        m("2025-11-13T03:45:00Z", -999),
    ]
    out = hourly_means(res)
    assert out["2025-11-13T02:30:00+00:00"] == (340 / 3, 3)
    assert out["2025-11-13T03:30:00+00:00"] == (50.0, 1)  # -999 (missing) dropped


def test_dry_run_log_masks_the_phone_number(monkeypatch, capsys):
    monkeypatch.setenv("WA_MODE", "dry_run")
    assert send(template_payload("919999990123", ["a", "b", "c", "d"], name="t", lang="en")) == "dry-run"
    out = capsys.readouterr().out
    assert "919999990123" not in out and "***0123" in out


def test_ingest_lookback_defaults_to_three_hours_and_caps_backfills(monkeypatch):
    monkeypatch.delenv("LOOKBACK_HOURS", raising=False)
    assert lookback_hours({}) == 3 and lookback_hours(None) == 3
    assert lookback_hours({"lookback_hours": 48}) == 48
    assert lookback_hours({"lookback_hours": 500}) == 72


def test_render_fills_the_template_body_word_for_word():
    d = {"kind": "clear_hour", "clear_hour": 13, "assembly_indoors": True}
    en = render(message_params(d, "KV RK Puram", date(2025, 11, 13), "en"), "en")
    assert en == (
        "ClearHour air update for KV RK Puram on Thu 13 Nov: Hold assembly indoors. "
        "Cleanest hour for outdoor activity: 1:00–2:00 PM. Reply 1 once you have moved outdoor activities."
    )
    hi = render(message_params(d, "केवी", date(2025, 11, 13), "hi"), "hi")
    assert hi.startswith("ClearHour वायु सूचना – केवी, गुरु 13 नवंबर: प्रार्थना सभा अंदर करें।")
    assert hi.endswith("गतिविधियाँ बदलने के बाद 1 लिखकर भेजें।")


def test_monitor_list_takes_the_pm25_sensor_that_reported_last(monkeypatch):
    # R K Puram lists a sensor retired in 2018 first; the live ingest must ask the one still reporting
    last = {35: "2018-02-21T21:15:00Z", 12234787: "2026-10-07T14:30:00Z"}

    def fake_get(path, params):
        return {"results": [{"datetimeLast": {"utc": last[int(path.rsplit("/", 1)[1])]}}]}

    monkeypatch.setattr(openaq_api, "get", fake_get)
    monkeypatch.setattr(openaq_api.time, "sleep", lambda s: None)  # skip the rate-limit pause
    assert openaq_api.newest_sensor([35, 12234787]) == 12234787
    assert openaq_api.newest_sensor([7]) == 7  # a single sensor needs no lookup
