"""相場の特徴量のハーネス（I/O なし）。AI にも、機械の判断（ロジック）にも、同じ数字を渡すための部品。

1分足（仲値）から、裁量のトレーダーが見る要素を機械的に計算する。AI に生の値段の並びだけを渡すと、
「最近動いた向きに続く」と読みがちなので、ふだんの揺れと比べた大きさ・長い足の流れ・節目までの距離を、
こちらで計算して渡す（含み損益を計算済みで渡すのと同じ考え方）。

  * 動き: 15分・1時間・4時間・24時間で何 pips 動いたか。ふだんの揺れ（ATR）の何倍か（``noise_ratio``）。
    1倍に届かない動きは「揺れの範囲」で、流れ（トレンド）の根拠にならない
  * 足ごと（1分・5分・1時間）: ATR（ふだんの1本の値幅）、RSI、移動平均（20・50本）との距離と向き、
    ボリンジャーバンドの位置（20本の平均から標準偏差の何倍か）
  * 節目: 今日・前の営業日の高値と安値、直近4時間の高値と安値、近くのキリの良い値段（.00 と .50）までの距離
  * 時間帯: 東京・ロンドン・ニューヨーク（冬時間の目安）
  * 日足（渡したときだけ）: 5日・20日の動きと、20日平均との距離

``summary`` は、同じ数字を人が読める文にしたもの（AI の材料と画面に使う）。
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import pandas as pd

PIP = 0.01
#: 動きがふだんの揺れの何倍なら「はっきりした流れ」と言えるか
CLEAR_TREND = 1.5
#: 冬時間の目安（日本時間）。夏時間は1時間早まる
SESSIONS = (("東京", 9, 15), ("ロンドン", 17, 26), ("ニューヨーク", 22, 31))


def _resample(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    return bars.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def atr(frame: pd.DataFrame, n: int = 14) -> float | None:
    if len(frame) < n + 1:
        return None
    prev = frame["close"].shift(1)
    tr = pd.concat([frame["high"] - frame["low"], (frame["high"] - prev).abs(), (frame["low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


def rsi(closes: pd.Series, n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    diff = closes.diff().tail(n)
    gain, loss = diff.clip(lower=0).mean(), (-diff.clip(upper=0)).mean()
    if loss == 0:
        return 100.0
    return float(100 - 100 / (1 + gain / loss))


def _ema(closes: pd.Series, n: int) -> pd.Series:
    return closes.ewm(span=n, adjust=False).mean()


def timeframe(frame: pd.DataFrame, pip: float = PIP) -> dict[str, Any]:
    """1つの足の特徴（足が足りなければ、計算できたものだけ）。"""
    closes = frame["close"]
    last = float(closes.iloc[-1])
    out: dict[str, Any] = {}
    a = atr(frame)
    if a is not None:
        out["atr_pips"] = round(a / pip, 1)
    r = rsi(closes)
    if r is not None:
        out["rsi"] = round(r, 0)
    for n in (20, 50):
        if len(closes) >= n:
            ema = _ema(closes, n)
            out[f"ema{n}_distance_pips"] = round((last - float(ema.iloc[-1])) / pip, 1)
            out[f"ema{n}_slope_pips"] = round((float(ema.iloc[-1]) - float(ema.iloc[-4])) / pip / 3, 2)
    if len(closes) >= 20:
        window = closes.tail(20)
        sd = float(window.std())
        if sd > 0:
            out["bollinger_z"] = round((last - float(window.mean())) / sd, 2)
    return out


def _change(bars: pd.DataFrame, now: pd.Timestamp, minutes: int, pip: float) -> float | None:
    past = bars[bars.index <= now - pd.Timedelta(minutes=minutes)]
    if past.empty:
        return None
    return round((float(bars["close"].iloc[-1]) - float(past["close"].iloc[-1])) / pip, 1)


def noise_label(ratio: float | None) -> str:
    if ratio is None:
        return "不明"
    if abs(ratio) < 1:
        return "揺れの範囲"
    if abs(ratio) < CLEAR_TREND:
        return "やや" + ("上向き" if ratio > 0 else "下向き")
    return "はっきり" + ("上向き" if ratio > 0 else "下向き")


def session_now(now: dt.datetime) -> str:
    hour = now.hour + now.minute / 60
    open_ = [name for name, start, end in SESSIONS if start <= hour < end or start <= hour + 24 < end]
    return "・".join(open_) if open_ else "時間帯の切れ目（参加者が少ない）"


def market_features(bars: pd.DataFrame, now: dt.datetime, *, daily_closes: list[float] | None = None,
                    pip: float = PIP) -> dict[str, Any]:
    """1分足（仲値。index は日本時間の naive、列 open/high/low/close/session）から特徴量を作る。"""
    if bars.empty:
        return {}
    stamp = pd.Timestamp(now.replace(tzinfo=None))
    bars = bars[bars.index <= stamp]
    last = float(bars["close"].iloc[-1])
    m5, h1 = _resample(bars, "5min"), _resample(bars, "1h")
    tf = {"1min": timeframe(bars.tail(120), pip), "5min": timeframe(m5.tail(120), pip), "1h": timeframe(h1.tail(120), pip)}
    # 動きを、その長さのふだんの揺れと比べる（1時間の揺れ ≒ 1時間足の ATR。長さが k 倍なら √k 倍）
    h1_atr = tf["1h"].get("atr_pips")
    moves = {}
    for label, minutes in (("15min", 15), ("1h", 60), ("4h", 240), ("24h", 1440)):
        change = _change(bars, stamp, minutes, pip)
        ratio = round(change / (h1_atr * math.sqrt(minutes / 60)), 2) if change is not None and h1_atr else None
        moves[label] = {"change_pips": change, "noise_ratio": ratio, "label": noise_label(ratio)}
    sessions = list(dict.fromkeys(bars["session"])) if "session" in bars else []
    today = bars[bars["session"] == sessions[-1]] if sessions else bars
    prev = bars[bars["session"] == sessions[-2]] if len(sessions) >= 2 else None
    recent = bars[bars.index > stamp - pd.Timedelta(hours=4)]

    def dist(level: float) -> float:
        return round((level - last) / pip, 1)

    levels = {
        "today_high": {"price": round(float(today["high"].max()), 3), "pips_away": dist(float(today["high"].max()))},
        "today_low": {"price": round(float(today["low"].min()), 3), "pips_away": dist(float(today["low"].min()))},
        "high_4h": {"price": round(float(recent["high"].max()), 3), "pips_away": dist(float(recent["high"].max()))},
        "low_4h": {"price": round(float(recent["low"].min()), 3), "pips_away": dist(float(recent["low"].min()))},
    }
    if prev is not None and not prev.empty:
        levels["prev_day_high"] = {"price": round(float(prev["high"].max()), 3), "pips_away": dist(float(prev["high"].max()))}
        levels["prev_day_low"] = {"price": round(float(prev["low"].min()), 3), "pips_away": dist(float(prev["low"].min()))}
    step = 50 * pip
    below, above = math.floor(last / step) * step, math.ceil(last / step) * step
    levels["round_below"] = {"price": round(below, 3), "pips_away": dist(below)}
    levels["round_above"] = {"price": round(above if above != below else above + step, 3),
                             "pips_away": dist(above if above != below else above + step)}
    out: dict[str, Any] = {
        "price": round(last, 3),
        "session": session_now(now),
        "moves": moves,
        "timeframes": tf,
        "levels": levels,
        "recent_1min_close": [round(float(v), 3) for v in bars["close"].tail(30)],
    }
    if daily_closes and len(daily_closes) >= 21:
        ma20 = sum(daily_closes[-20:]) / 20
        out["daily"] = {"change_5d_pips": round((last - daily_closes[-5]) / pip, 1),
                        "change_20d_pips": round((last - daily_closes[-20]) / pip, 1),
                        "ma20_distance_pips": round((last - ma20) / pip, 1)}
    out["summary"] = summarize(out)
    return out


def summarize(f: dict[str, Any]) -> list[str]:
    """特徴量を、人が読める短い文にする。"""
    lines = []
    names = {"15min": "15分", "1h": "1時間", "4h": "4時間", "24h": "24時間"}
    moves = [f"{names[k]} {v['change_pips']:+.1f} pips（{v['label']}）" for k, v in f["moves"].items()
             if v["change_pips"] is not None]
    if moves:
        lines.append("動き: " + "、".join(moves))
    h1, m5 = f["timeframes"]["1h"], f["timeframes"]["5min"]
    if "atr_pips" in h1:
        lines.append(f"ふだんの揺れ: 1時間足 {h1['atr_pips']:g} pips・5分足 {m5.get('atr_pips', 0):g} pips")
    if "rsi" in m5 or "rsi" in h1:
        lines.append(f"RSI: 5分足 {m5.get('rsi', '—')}・1時間足 {h1.get('rsi', '—')}（70以上は買われすぎ、30以下は売られすぎの目安）")
    if "ema20_distance_pips" in h1:
        lines.append(f"1時間足の20本平均から {h1['ema20_distance_pips']:+.1f} pips（平均の向き {h1['ema20_slope_pips']:+.2f} pips/本）")
    lv = f["levels"]
    lines.append(f"節目: 今日の高値 {lv['today_high']['price']}（{lv['today_high']['pips_away']:+.1f}）・"
                 f"安値 {lv['today_low']['price']}（{lv['today_low']['pips_away']:+.1f}）、"
                 f"キリの良い値段 {lv['round_above']['price']}（{lv['round_above']['pips_away']:+.1f}）・"
                 f"{lv['round_below']['price']}（{lv['round_below']['pips_away']:+.1f}）")
    if "daily" in f:
        d = f["daily"]
        lines.append(f"日足: 5日 {d['change_5d_pips']:+.1f} pips・20日 {d['change_20d_pips']:+.1f} pips、"
                     f"20日平均から {d['ma20_distance_pips']:+.1f} pips")
    lines.append(f"時間帯: {f['session']}")
    return lines
