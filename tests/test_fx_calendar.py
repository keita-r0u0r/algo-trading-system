from __future__ import annotations

import datetime as dt

import httpx

from jqbt.application import fx_calendar
from jqbt.infrastructure import econ_calendar

JST = fx_calendar.JST
RAW = [
    {"date": "2026-11-06T08:30:00-05:00", "country": "USD", "title": "Non-Farm Employment Change", "impact": "High",
     "forecast": "65K", "previous": "29K"},
    {"date": "2026-11-06T09:00:00-05:00", "country": "CAD", "title": "Unemployment Rate", "impact": "High"},
    {"date": "2026-11-06T10:00:00-05:00", "country": "USD", "title": "Prelim UoM", "impact": "Medium"},
    {"date": "2026-11-06T11:00:00-05:00", "country": "USD", "title": "Bank Holiday", "impact": "Holiday"},
]
NFP = dt.datetime(2026, 11, 6, 22, 30, tzinfo=JST)


def test_parse_keeps_medium_and_high():
    events = fx_calendar.parse(RAW)
    assert [e.title for e in events] == ["Non-Farm Employment Change", "Unemployment Rate", "Prelim UoM"]
    assert events[0].at == NFP


def test_upcoming_only_our_currencies():
    rows = fx_calendar.upcoming(fx_calendar.parse(RAW), NFP - dt.timedelta(minutes=40), ["USD_JPY"])
    assert [r["title"] for r in rows] == ["Non-Farm Employment Change", "Prelim UoM"]  # CAD は関係ない
    assert rows[0]["minutes_from_now"] == 40 and rows[0]["at_jst"] == "11-06 22:30" and rows[0]["impact"] == "重要"


def test_quiet_around_high_impact():
    events = fx_calendar.parse(RAW)
    assert fx_calendar.quiet_reason(events, "USD_JPY", NFP - dt.timedelta(minutes=5))
    assert "後" in fx_calendar.quiet_reason(events, "USD_JPY", NFP + dt.timedelta(minutes=10))
    assert fx_calendar.quiet_reason(events, "USD_JPY", NFP + dt.timedelta(minutes=20)) is None
    assert fx_calendar.quiet_reason(events, "USD_JPY", NFP - dt.timedelta(minutes=30)) is None
    assert fx_calendar.quiet_reason(events, "EUR_JPY", NFP) is None  # 米国の発表は EUR_JPY に関係しない
    # 中くらいの発表では休まない
    assert fx_calendar.quiet_reason(events, "USD_JPY", NFP + dt.timedelta(minutes=90)) is None


def test_load_uses_cache_and_keeps_old_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "fx_calendar.json"
    now = dt.datetime(2026, 11, 3, 15, 0, tzinfo=JST)
    calls = []

    def ok(url, **_):
        calls.append(url)
        return httpx.Response(200, json=RAW, request=httpx.Request("GET", url))

    monkeypatch.setattr(econ_calendar.httpx, "get", ok)
    assert len(econ_calendar.load(path, now)["events"]) == 4
    econ_calendar.load(path, now + dt.timedelta(minutes=30))
    assert len(calls) == 1  # 1時間以内は取りに行かない

    def fail(url, **_):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(econ_calendar.httpx, "get", fail)
    data = econ_calendar.load(path, now + dt.timedelta(hours=2))
    assert len(data["events"]) == 4 and "ConnectError" in data["error"]
    assert data["fetched_at"] == now.isoformat(timespec="seconds")  # 取れた時刻は古いまま（健康診断が気づく）
