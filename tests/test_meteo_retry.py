"""Open-Meteo calls retry brief outages and give up at once on real errors."""

import pytest
import requests

from clearhour import meteo


class _Resp:
    def __init__(self, status: int):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return {"hourly": {"time": ["2026-10-08T00:00"], "pm2_5": [50.0]}}


def _fake_get(statuses: list[int], calls: list[int]):
    def get(url, params=None, timeout=None):
        calls.append(1)
        return _Resp(statuses[min(len(calls), len(statuses)) - 1])

    return get


def test_a_503_is_retried(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(meteo.requests, "get", _fake_get([503, 503, 200], calls))
    monkeypatch.setattr(meteo.time, "sleep", lambda s: None)
    assert meteo._get("https://example.test", {}).status_code == 200 and len(calls) == 3


def test_a_400_fails_at_once(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(meteo.requests, "get", _fake_get([400], calls))
    monkeypatch.setattr(meteo.time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError):
        meteo._get("https://example.test", {})
    assert len(calls) == 1


def test_a_lasting_outage_raises_after_four_tries(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(meteo.requests, "get", _fake_get([503], calls))
    monkeypatch.setattr(meteo.time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError):
        meteo._get("https://example.test", {})
    assert len(calls) == 4
