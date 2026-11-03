"""短期売買のペーパー運用（application/fx_scalp.py）: 値段1つずつの判断と、スプレッド込みの仮想の約定。"""

from __future__ import annotations

import datetime as dt

from jqbt.application.fx_scalp import RULES, ScalpAccount, ScalpRule, TickWindow, on_tick, restore, summary

JST = dt.timezone(dt.timedelta(hours=9))
T0 = dt.datetime(2026, 11, 3, 12, 0, tzinfo=JST)


def feed(account, ticks):
    window = TickWindow(account.rule.trigger_seconds)
    events = []
    for seconds, bid, ask in ticks:
        at = T0 + dt.timedelta(seconds=seconds)
        window.add("USD_JPY", at, (bid + ask) / 2)
        events += on_tick(account, window, "USD_JPY", at, bid, ask)
    return events


def test_fade_sells_after_a_jump_and_takes_profit_after_spread():
    account = ScalpAccount(ScalpRule("f", "fade", take_pips=3, stop_pips=5))
    events = feed(account, [(0, 158.000, 158.002), (30, 158.060, 158.062),  # 30秒で +6 pips → 売り（bid 158.060）
                            (40, 158.040, 158.042), (50, 158.028, 158.030)])  # ask 158.030 で利確の線（158.030）
    opened, closed = events
    assert opened["side"] == "SELL" and opened["price"] == 158.060 and opened["take"] == 158.030
    assert closed["reason"] == "利確" and closed["pips"] == 3.0
    assert closed["pnl"] == 30.0 and closed["fee"] == 0  # 1,000通貨 × 3 pips = 30円（手数料は0円）


def test_follow_buys_and_stops_out():
    account = ScalpAccount(ScalpRule("g", "follow", take_pips=5, stop_pips=3))
    events = feed(account, [(0, 158.000, 158.002), (20, 158.060, 158.062), (25, 158.031, 158.033)])
    opened, closed = events
    assert opened["side"] == "BUY" and opened["price"] == 158.062 and closed["reason"] == "損切り"
    assert closed["pips"] == -3.0 and account.position is None


def test_no_entry_when_spread_is_wide_or_move_is_small_or_cooling_down():
    wide = ScalpAccount(ScalpRule("w", "fade"))
    assert feed(wide, [(0, 158.000, 158.002), (30, 158.060, 158.080)]) == []  # スプレッド 2 pips
    small = ScalpAccount(ScalpRule("s", "fade"))
    assert feed(small, [(0, 158.000, 158.002), (30, 158.030, 158.032)]) == []  # 3 pips
    cool = ScalpAccount(ScalpRule("c", "fade", max_hold_seconds=10))
    events = feed(cool, [(0, 158.000, 158.002), (30, 158.060, 158.062), (41, 158.060, 158.062), (60, 158.130, 158.132)])
    assert [e["kind"] for e in events] == ["scalp_open", "scalp_close"]  # 時間切れのあと2分は建てない
    assert events[1]["reason"] == "0分たった" or "たった" in events[1]["reason"]


def test_restore_and_summary():
    account = ScalpAccount(RULES[0])
    events = feed(account, [(0, 158.000, 158.002), (30, 158.060, 158.062), (50, 158.028, 158.030)])
    again = restore(RULES[0], events)
    assert round(again.cash, 2) == round(account.cash, 2) and again.trades == 1 and again.wins == 1
    s = summary(events, RULES[0].name)
    assert s["trades"] == 1 and s["win_rate"] == 1.0 and s["avg_pips"] == 3.0


def test_old_fee_is_given_back():
    """scalp-v1 で誤って引いた手数料は、成績と口座の作り直しで戻す。"""
    events = [{"kind": "scalp_close", "account": "scalp_fade", "pnl": -4.41, "fee": 4.41, "pips": 0.0,
               "cash": 9995.59, "version": "scalp-v1"},
              {"kind": "scalp_close", "account": "scalp_fade", "pnl": 30.0, "fee": 0.0, "pips": 3.0,
               "cash": 10030.0, "version": "scalp-v2"}]
    s = summary(events, "scalp_fade")
    assert s["pnl"] == 30.0 and s["fees"] == 0
    account = restore(RULES[0], events)
    assert account.cash == 10030.0 and account.trades == 2 and account.wins == 1
