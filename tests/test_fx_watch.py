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


def test_watch_snapshot_shows_distance_to_wake():
    import datetime as dt

    from jqbt.application.fx_watch import PriceHistory, WatchConfig, watch_snapshot

    jst = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime(2026, 11, 3, 12, 0, tzinfo=jst)
    h = PriceHistory(dt.timedelta(minutes=5))
    h.add("USD_JPY", now - dt.timedelta(minutes=4), 157.900)
    h.add("USD_JPY", now, 157.950)
    snap = watch_snapshot(now, h, [{"venue": "live", "account": "ai", "symbol": "USD_JPY", "stop": 157.676, "take": None}],
                          WatchConfig(), last_wake=None, fired_sessions=set())
    assert snap["symbols"]["USD_JPY"] == {"price": 157.95, "move_pips": 5.0, "to_wake_pips": 10.0}
    assert snap["positions"][0]["pips_to_stop"] == 27.4 and "pips_to_take" not in snap["positions"][0]
    assert snap["next_session"] == "17:00" and snap["cooldown_until"] is None


LONG = {"venue": "live", "account": "ai", "symbol": "GBP_JPY", "side": "BUY", "price": 212.217,
        "stop": 212.017, "take": 212.617, "opened_at": "2026-11-03T21:26:00+09:00"}


def gbp(points):
    h = PriceHistory(dt.timedelta(minutes=CFG.window_minutes))
    for t, mid in points:
        h.add("GBP_JPY", t, mid)
    return h


def test_loss_review_fires_once_per_level():
    fired: set[str] = set()
    # 損切りまで 20 pips。1/3（約 -6.7 pips）を越えたら1回だけ見直す
    h = gbp([(at(21, 32), 212.20), (at(21, 32, 30), 212.145)])
    reason = wake(at(21, 32, 30), h, [LONG], fired=fired)
    assert reason.startswith("見直し: ai の建玉の含み損が損切りまでの33%")
    assert wake(at(21, 32, 40), h, [LONG], fired=fired) == ""
    h.add("GBP_JPY", at(21, 33), 212.08)  # 2/3（約 -13.3 pips）
    assert "67%" in wake(at(21, 33), h, [LONG], fired=fired)


def test_gain_giveback_wakes():
    fired: set[str] = set()
    peaks: dict[str, float] = {}
    h = gbp([(at(21, 32), 212.30)])  # +8.3 pips（利確までの1/3 = 13.3 pips にはまだ）
    assert reason_to_wake(at(21, 32), h, [LONG], CFG, last_wake=None, fired_sessions=fired, peaks=peaks) == ""
    h.add("GBP_JPY", at(21, 33), 212.26)  # +4.3 pips: 最高 8.3 の 40% 以上を失った
    reason = reason_to_wake(at(21, 33), h, [LONG], CFG, last_wake=None, fired_sessions=fired, peaks=peaks)
    assert reason.startswith("含み益の目減り: ai の建玉が最高 +8.3 pips から +4.3 pips に減った")
    assert reason_to_wake(at(21, 33, 20), h, [LONG], CFG, last_wake=None, fired_sessions=fired, peaks=peaks) == ""


def test_small_gains_do_not_trigger_giveback():
    peaks: dict[str, float] = {}
    h = gbp([(at(21, 32), 212.25), (at(21, 33), 212.22)])  # 最高 +3.3 pips（5 pips 未満）
    assert reason_to_wake(at(21, 33), h, [LONG], CFG, last_wake=None, fired_sessions=set(), peaks=peaks) == ""


def test_thesis_invalidation_and_slow_progress():
    pos = {**LONG, "thesis": {"invalidation_price": 212.10, "target_price": 212.517, "hold_minutes": 60}}
    fired: set[str] = set()
    h = gbp([(at(21, 30), 212.15)])  # 根拠が崩れる値段の手前。損切りまでの1/3（-6.7）に届かない -6.7 pips
    assert wake(at(21, 30), h, [pos], fired=fired) == ""
    h.add("GBP_JPY", at(21, 31), 212.099)
    reasons = [wake(at(21, 31), h, [pos], fired=fired) for _ in range(3)]
    assert any(r.startswith("予想から外れた: ai の建玉が根拠が崩れる値段（212.1）を越えた") for r in reasons)
    # 目標まで 30 pips。30分（半分）たって +3 pips（10%）なら遅い
    slow = {**LONG, "opened_at": "2026-11-03T21:00:00+09:00", "thesis": {"target_price": 212.517, "hold_minutes": 60}}
    h2 = gbp([(at(21, 31), 212.247)])
    assert "半分たっても" in wake(at(21, 31), h2, [slow], fired=set())
