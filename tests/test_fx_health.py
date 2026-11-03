"""健康診断（application/fx_health.py）: 動いているはずのものが、本当に動いているか。"""

from __future__ import annotations

import datetime as dt

from jqbt.application.fx_health import check_all, market_open, worst

JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 11, 3, 10, 0, tzinfo=JST)  # 火曜
M = dt.timedelta(minutes=1)
ACCOUNTS = ("ai", "carry_trend_USD_JPY")


def iso(t):
    return t.isoformat(timespec="seconds")


def healthy_watch():
    return {"trading": True, "version": "abc", "last_tick": iso(NOW - M), "last_ok_cycle": iso(NOW - 2 * M), "cycles": 5}


def decisions(ai_extra, rule_extra=None):
    return [{"kind": "live_decision", "at": iso(NOW - 3 * M), "account": "ai", "decision": {"action": "hold"}, **ai_extra},
            {"kind": "live_decision", "at": iso(NOW - 3 * M), "account": "carry_trend_USD_JPY",
             "decision": {"action": "hold"}, **(rule_extra if rule_extra is not None else {"logic": {"target": 1}})}]


def run(watch=None, events=None, **kw):
    base = dict(live_enabled=True, killed=False, paper_paused=False)
    base.update(kw)
    return check_all(NOW, watch=healthy_watch() if watch is None else watch,
                     live_events=decisions({"model": "gemini"}) if events is None else events, accounts=ACCOUNTS, **base)


def test_all_healthy():
    assert worst(run()) == "ok"


def test_ai_default_holds_are_caught():
    """AI を呼べないまま「持ち続ける」が並んでいる状態を、異常として出す（9時間気づかなかった件）。"""
    findings = run(events=decisions({"error": "JSONDecodeError: Expecting value"}))
    ai = next(f for f in findings if f.name == "ai")
    assert ai.state == "bad" and "JSONDecodeError" in ai.text
    assert findings[0].state == "bad"  # 悪いものが先頭


def test_watcher_problems():
    assert next(f for f in run(watch={}) if f.name == "見張り役").state == "bad"
    restarting = {**healthy_watch(), "last_tick": iso(NOW - 5 * M)}
    f = next(f for f in run(watch=restarting) if f.name == "見張り役")
    assert f.state == "warn" and "作り直し" in f.text and "待つだけ" in f.action and "09:55" in f.text
    down = {**healthy_watch(), "last_tick": iso(NOW - 30 * M)}
    f = next(f for f in run(watch=down) if f.name == "見張り役")
    assert f.state == "bad" and f.impact and f.action
    booting = {**healthy_watch(), "last_ok_cycle": None, "started_at": iso(NOW - 2 * M)}
    assert next(f for f in run(watch=booting) if f.name == "見張り役").state == "info"
    rejected = {**healthy_watch(), "last_ok_cycle": iso(NOW - 60 * M)}
    assert "口座に届いていない" in next(f for f in run(watch=rejected) if f.name == "見張り役").text


def test_switches_and_paper():
    assert next(f for f in run(killed=True) if f.name == "自動売買").state == "bad"
    paper = next(f for f in run(paper_paused=True) if f.name == "ペーパー運用")
    assert paper.state == "info"  # 実際の売買には関係ないので、知らせるだけ


def test_account_errors():
    events = decisions({"model": "gemini"}) + [{"kind": "live_ready", "at": iso(NOW - M), "ok": False, "note": "ERR-5012"}]
    assert any(f.name == "口座" and f.state == "bad" for f in run(events=events))


def test_market_closed_is_not_an_alarm():
    saturday = dt.datetime(2026, 11, 7, 12, 0, tzinfo=JST)
    assert not market_open(saturday) and market_open(NOW)
    assert not market_open(dt.datetime(2026, 11, 9, 6, 0, tzinfo=JST)) and market_open(dt.datetime(2026, 11, 9, 7, 0, tzinfo=JST))
    findings = check_all(saturday, watch={**healthy_watch(), "last_tick": iso(NOW)}, live_events=[], accounts=ACCOUNTS,
                         live_enabled=True, killed=False, paper_paused=False)
    assert worst(findings) == "info"


def test_calendar_stale_is_a_warning():
    from jqbt.application.fx_health import check_all

    now = dt.datetime(2026, 11, 4, 15, 0, tzinfo=JST)
    common = dict(watch={}, live_events=[], accounts=[], live_enabled=True, killed=False, paper_paused=False)
    old = check_all(now, **common, calendar={"fetched_at": "2026-11-04T10:00:00+09:00", "error": "x"})
    assert [f.state for f in old if f.name == "経済指標の予定"] == ["warn"]
    fresh = check_all(now, **common, calendar={"fetched_at": "2026-11-04T14:30:00+09:00"})
    assert [f.state for f in fresh if f.name == "経済指標の予定"] == ["ok"]
    assert not [f for f in check_all(now, **common) if f.name == "経済指標の予定"]
