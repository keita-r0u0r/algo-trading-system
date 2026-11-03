import datetime as dt

import pandas as pd

from jqbt.application import fx_setups as fs

JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 11, 4, 10, 0, tzinfo=JST)


def features(r4=1.5, c24=60.0, slope=2.0, z=-1.0, turn=0.3, atr=20.0, price=158.0):
    return {"price": price,
            "moves": {"4h": {"noise_ratio": r4}, "24h": {"change_pips": c24}},
            "timeframes": {"1h": {"ema50_slope_pips": slope, "atr_pips": atr},
                           "5min": {"bollinger_z": z}, "1min": {"ema20_slope_pips": turn}}}


def test_trend_needs_all_three_in_the_same_direction():
    assert fs.trend(features())[0] == 1
    assert fs.trend(features(r4=-1.5, c24=-60, slope=-2))[0] == -1
    assert fs.trend(features(r4=0.7))[0] == 0  # 揺れの範囲
    assert fs.trend(features(c24=-10))[0] == 0  # 24時間は逆
    assert fs.trend(features(slope=-0.5))[0] == 0  # 1時間足の平均は逆


def test_entry_buys_a_pullback_that_turns_back_up():
    c, why = fs.entry("USD_JPY", features())
    assert c is not None and c.side == 1 and c.setup == "pullback"
    assert c.stop_pips == 16.0 and c.invalidation_price == 157.84  # 揺れ20 × 0.8
    d = c.decision(fs.HarnessRules())
    assert d["action"] == "open_long" and d["exit_method"] == "chandelier" and d["take_pips"] == 0  # 追いかける手法は利確なし
    d = c.decision(fs.HarnessRules(), "target")
    assert d["take_pips"] == 48.0 and d["target_price"] == 158.48 and d["exit_method"] == "target"
    assert c.decision(fs.HarnessRules(), "nope")["exit_method"] == "chandelier"


def test_entry_never_fades_the_trend_or_chases_it():
    assert fs.entry("USD_JPY", features(z=0.8))[0] is None  # 押していない（流れの途中）
    assert fs.entry("USD_JPY", features(turn=-0.2))[0] is None  # 押しがまだ止まっていない
    # 下げの流れでは、戻り（z が正）を売る
    c, _ = fs.entry("USD_JPY", features(r4=-1.5, c24=-60, slope=-2, z=1.0, turn=-0.3))
    assert c is not None and c.side == -1


def test_entry_respects_events_and_reentry_blocks():
    assert fs.entry("USD_JPY", features(), quiet="重要な発表")[0] is None
    assert fs.entry("USD_JPY", features(), blocked="同じ向きで損をした")[0] is None


def test_stop_is_clipped_to_the_range():
    assert fs.entry("USD_JPY", features(atr=5))[0].stop_pips == 12.0
    assert fs.entry("USD_JPY", features(atr=100))[0].stop_pips == 40.0


def test_best_entry_picks_the_strongest_trend_and_explains_each_symbol():
    c, seen = fs.best_entry({"USD_JPY": features(r4=1.2), "GBP_JPY": features(r4=2.5, price=212.0), "AUD_JPY": {}})
    assert c.symbol == "GBP_JPY"
    assert set(seen) == {"USD_JPY", "GBP_JPY"}


def test_best_entry_blocks_only_the_losing_direction():
    blocked = {"USD_JPY": {1: "損をした"}}
    assert fs.best_entry({"USD_JPY": features()}, blocked=blocked)[0] is None
    down = features(r4=-1.5, c24=-60, slope=-2, z=1.0, turn=-0.3)
    assert fs.best_entry({"USD_JPY": down}, blocked=blocked)[0] is not None


def path(*highs, low=157.95):
    idx = pd.date_range("2026-11-04 10:00", periods=len(highs), freq="1min")
    return pd.DataFrame({"high": list(highs), "low": [low] * len(highs)}, index=idx)


def check(**kw):
    args = dict(side=1, entry_price=158.0, stop_pips=16.0, price=158.0, bars=path(158.0),
                opened_at=NOW, now=NOW + dt.timedelta(minutes=30), f=features(), method="chandelier")
    args.update(kw)
    return fs.exit_check(**args)


def test_chandelier_trails_from_the_high_by_atr():
    r = check(price=158.20, bars=path(158.0, 158.50))  # 線 = 158.50 − 2 × 20 pips = 158.10
    assert not r.close and r.exit_level == 158.10
    r = check(price=158.09, bars=path(158.0, 158.50))
    assert r.close and "158.1" in r.reason


def test_trail_never_sits_below_the_initial_stop():
    assert check(price=158.05, bars=path(158.0, 158.05)).exit_level is None  # 158.05 − 0.40 は最初の損切りより下


def test_breakeven_and_percent_peak_and_target():
    assert check(method="breakeven", price=158.10, bars=path(158.0, 158.20)).exit_level is None  # 1.5R に届かない
    r = check(method="breakeven", price=158.005, bars=path(158.0, 158.25))
    assert r.close and r.exit_level == 158.01
    r = check(method="percent_peak", price=158.09, bars=path(158.0, 158.20))  # 最大 +20 の半分 +10
    assert r.close and r.exit_level == 158.10
    assert check(method="target", price=158.01, bars=path(158.0, 158.40)).exit_level is None


def test_structure_trails_under_recent_5min_lows():
    idx = pd.date_range("2026-11-04 10:00", periods=40, freq="1min")
    lows = [158.00 + 0.005 * i for i in range(40)]
    bars = pd.DataFrame({"high": [x + 0.05 for x in lows], "low": lows}, index=idx)
    r = check(method="structure", price=158.20, bars=bars)
    assert r.exit_level is not None and 158.0 < r.exit_level < 158.2


def test_exit_when_the_trend_turns_against_the_position():
    r = check(price=157.95, f=features(r4=-1.5, c24=-60, slope=-2))
    assert r.close and "流れが逆" in r.reason


def test_exit_when_time_runs_out_without_progress():
    r = check(price=158.05, bars=path(158.0, 158.08), now=NOW + dt.timedelta(hours=7))
    assert r.close and "時間切れ" in r.reason
    assert not check(price=158.05, bars=path(158.0, 158.08), now=NOW + dt.timedelta(hours=5)).close


def test_peak_pips_uses_highs_since_open_for_longs_and_lows_for_shorts():
    idx = pd.date_range("2026-11-04 09:58", periods=5, freq="1min")
    bars = pd.DataFrame({"high": [158.5, 158.05, 158.20, 158.10, 158.0],
                         "low": [157.5, 157.95, 157.90, 157.80, 157.9]}, index=idx)
    assert round(fs.peak_pips(bars, 1, 158.0, NOW), 1) == 20.0  # 09:58 の高値は建てる前なので見ない
    assert round(fs.peak_pips(bars, -1, 158.0, NOW), 1) == 20.0


def test_closed_trades_and_reentry_blocks_from_the_live_log():
    events = [
        {"kind": "live_open", "account": "harness", "position_id": 1, "symbol": "USD_JPY", "side": "BUY",
         "at": "2026-11-04T08:00:00+09:00"},
        {"kind": "execution", "settleType": "CLOSE", "positionId": 1, "lossGain": "-40",
         "at": "2026-11-04T09:00:00+09:00"},
        {"kind": "live_open", "account": "ai", "position_id": 2, "symbol": "GBP_JPY", "side": "SELL", "at": "x"},
        {"kind": "execution", "settleType": "CLOSE", "positionId": 2, "lossGain": "-90", "at": "2026-11-04T09:30:00+09:00"},
    ]
    trades = fs.closed_trades(events, "harness")
    assert trades == [{"symbol": "USD_JPY", "side": 1, "pnl": -40.0, "opened_at": "2026-11-04T08:00:00+09:00",
                       "closed_at": "2026-11-04T09:00:00+09:00"}]
    assert 1 in fs.reentry_blocks(trades, NOW)["USD_JPY"]
    assert fs.reentry_blocks(trades, NOW + dt.timedelta(hours=2)) == {}
