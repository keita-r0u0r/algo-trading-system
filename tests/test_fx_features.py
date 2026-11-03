from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from jqbt.application.fx_features import market_features, noise_label, session_now

JST = dt.timezone(dt.timedelta(hours=9))


def bars(drift_per_min: float, minutes: int = 3 * 1440, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-11-01 07:00", periods=minutes, freq="1min")
    close = 150 + np.cumsum(rng.normal(drift_per_min, 0.01, minutes))
    frame = pd.DataFrame({"open": close, "high": close + 0.005, "low": close - 0.005, "close": close}, index=idx)
    frame["session"] = [d.date() for d in (idx - pd.Timedelta(hours=7))]
    return frame


def test_noise_and_trend_are_told_apart():
    now = dt.datetime(2026, 11, 4, 6, 59, tzinfo=JST)
    flat = market_features(bars(0.0), now)
    up = market_features(bars(0.004), now)
    assert flat["moves"]["4h"]["label"] in ("揺れの範囲", "やや上向き", "やや下向き")
    assert up["moves"]["4h"]["label"] == "はっきり上向き" and up["moves"]["4h"]["noise_ratio"] > 1.5
    assert up["timeframes"]["1h"]["rsi"] > 60
    assert len(up["recent_1min_close"]) == 30 and up["summary"][0].startswith("動き:")


def test_levels_and_round_numbers():
    now = dt.datetime(2026, 11, 4, 6, 59, tzinfo=JST)
    f = market_features(bars(0.0), now)
    lv = f["levels"]
    assert lv["today_low"]["pips_away"] <= 0 <= lv["today_high"]["pips_away"]
    assert lv["round_below"]["price"] <= f["price"] < lv["round_above"]["price"]
    assert "prev_day_high" in lv


def test_daily_part_only_with_enough_closes():
    now = dt.datetime(2026, 11, 4, 6, 59, tzinfo=JST)
    assert "daily" not in market_features(bars(0.0), now, daily_closes=[150.0] * 10)
    assert "daily" in market_features(bars(0.0), now, daily_closes=[150.0] * 30)


def test_labels():
    assert noise_label(0.5) == "揺れの範囲" and noise_label(-2) == "はっきり下向き" and noise_label(None) == "不明"
    assert session_now(dt.datetime(2026, 11, 4, 23, 0, tzinfo=JST)) == "ロンドン・ニューヨーク"
