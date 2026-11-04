import datetime as dt

import pandas as pd

from jqbt.application import fx_spread as fs

JST = dt.timezone(dt.timedelta(hours=9))


def test_limit_scales_with_the_stop():
    assert not fs.judge(1.5, stop_pips=12, hold_minutes=60, normal=0.5).ok  # 上限は 1.2
    assert fs.judge(1.5, stop_pips=20, hold_minutes=60, normal=0.5).ok      # 上限は 2.0（短期は待たない）
    v = fs.judge(17.9, stop_pips=30, hold_minutes=180, normal=0.9)
    assert not v.ok and "上限" in v.text                                    # 今朝の AI の売り
    assert fs.judge(0.8, stop_pips=None, hold_minutes=None, normal=None).ok  # 損切り幅が無くても 1 pip までは通す


def test_patient_trades_wait_for_a_normal_spread():
    v = fs.judge(6.0, stop_pips=150, hold_minutes=None, normal=4.7, typical=0.9)
    assert not v.ok and "待つ" in v.text and "5.1" in v.text  # 今朝8:50のポンド円の金利差: 朝はいつも広いが、待てば昼の幅
    assert fs.judge(1.2, stop_pips=150, hold_minutes=None, normal=0.9).ok  # ふだんに近ければ建てる
    assert fs.judge(6.0, stop_pips=150, hold_minutes=None, normal=None).ok  # ふだんがわからなければ上限だけ


def test_normal_spread_uses_the_same_hour_of_past_days():
    idx = pd.date_range("2026-11-02 00:00", "2026-11-04 08:59", freq="1min")
    bars = pd.DataFrame({"spread": [5.0 if t.hour in (7, 8) else 0.5 for t in idx]}, index=idx)
    assert fs.normal_spread(bars, dt.datetime(2026, 11, 4, 8, 30, tzinfo=JST)) == 5.0
    assert fs.normal_spread(bars, dt.datetime(2026, 11, 4, 10, 0, tzinfo=JST)) == 0.5
    assert fs.normal_spread(None, dt.datetime(2026, 11, 4, 10, 0, tzinfo=JST)) is None
    assert fs.normal_spread(bars, dt.datetime(2026, 11, 4, 8, 30, tzinfo=JST), same_hour=False) == 0.5
