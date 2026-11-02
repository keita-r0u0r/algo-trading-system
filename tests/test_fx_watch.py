"""値段の見張り: 機械的な条件で判断の回を起こすか。"""

from __future__ import annotations

import datetime as dt

from jqbt.application.fx_watch import PriceHistory, WatchConfig, reason_to_wake

JST = dt.timezone(dt.timedelta(hours=9))
CFG = WatchConfig()


def at(hh, mm, ss=0):
    return dt.datetime(2026, 11, 3, hh, mm, ss, tzinfo=JST)  # 火曜


def history(points):
    h = PriceHistory(dt.timedelta(minutes=CFG.window_minutes))
    for t, mid in points:
        h.add("USD_JPY", t, mid)
    return h


def wake(now, h, positions=(), last=None, fired=None):
    return reason_to_wake(now, h, list(positions), CFG, last_wake=last, fired_sessions=fired if fired is not None else set())


def test_sudden_move_wakes():
    h = history([(at(10, 5), 157.00), (at(10, 7), 157.20)])
    assert wake(at(10, 7), h).startswith("急変: USD_JPY が 5分で +20.0 pips")


def test_small_move_and_old_points_do_not_wake():
    h = history([(at(10, 0), 156.50), (at(10, 6), 157.00), (at(10, 7), 157.05)])  # 10:00 は窓の外で捨てる
    assert wake(at(10, 7), h) == ""


def test_near_stop_wakes():
    h = history([(at(10, 6), 157.00), (at(10, 7), 157.02)])
    pos = [{"account": "ai", "symbol": "USD_JPY", "stop": 157.00, "take": 157.50}]
    assert "損切り" in wake(at(10, 7), h, pos)


def test_cooldown_and_scheduled_guard():
    h = history([(at(10, 5), 157.00), (at(10, 7), 157.30)])
    assert wake(at(10, 7), h, last=at(10, 6)) == ""  # 直前に起こした
    h2 = history([(at(10, 12), 157.00), (at(10, 14), 157.30)])
    assert wake(at(10, 14), h2) == ""  # 10:15 の定期の回が近い


def test_session_open_fires_once():
    h = history([(at(17, 2), 157.00)])
    fired = set()
    assert "17:00" in wake(at(17, 2), h, fired=fired)
    assert wake(at(17, 7), h, fired=fired) == ""
