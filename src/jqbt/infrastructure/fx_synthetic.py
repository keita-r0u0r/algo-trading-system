"""ダミーの FX 分足（仲値）。**成績の根拠にはならない。仕組みの検証用。**

値動きの性質は、ここで決めて作る。だから、ある戦略がこのデータで勝つかどうかは、
作り方でほぼ決まる。使い道は次の3つに限る。

  * ``random``  性質なし（ランダムウォーク）。どの戦略も、コスト分だけ負けるはず
  * ``meanrev`` 平均回帰を入れる。逆張りが勝つはず（コストを上げると消えるはず）
  * ``trend``   値動きに勢い（自己相関）を入れる。順張りが勝つはず
  * ``jumps``   ランダム + 急変と週明けの窓開け。損切り・ロスカットの扱いを試す

これらで「入れた性質を拾えるか」「入れていないのに勝ちと判定しないか」を確かめる。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

REGIMES = ("random", "meanrev", "trend", "jumps")


def generate(
    days: int = 20,
    regime: str = "random",
    seed: int = 0,
    start_price: float = 150.0,
    annual_vol: float = 0.10,
    bar_minutes: int = 1,
) -> pd.DataFrame:
    """平日 ``days`` 日ぶんの分足。1日は 00:00〜23:59（日の区切りの時計）。"""
    if regime not in REGIMES:
        raise ValueError(f"未知の regime: {regime}（{', '.join(REGIMES)}）")
    rng = np.random.default_rng(seed)
    per_day = 24 * 60 // bar_minutes
    n = days * per_day
    sigma = annual_vol / np.sqrt(252 * per_day)

    shocks = rng.standard_normal(n) * sigma
    if regime == "trend":
        # 値動きに勢い: r_t = φ r_{t-1} + ε（φ>0 で順張りが効く）
        phi, r = 0.25, np.empty(n)
        r[0] = shocks[0]
        for i in range(1, n):
            r[i] = phi * r[i - 1] + shocks[i]
        log_returns = r
    elif regime == "meanrev":
        # 平均回帰: 価格はゆっくり動く基準値へ引き戻される（半減期は約20本）
        kappa = np.log(2) / 20
        anchor = np.cumsum(rng.standard_normal(n) * sigma * 0.3)
        x, deviation = np.empty(n), 0.0
        for i in range(n):
            deviation += -kappa * deviation + shocks[i] * 2.0
            x[i] = anchor[i] + deviation
        log_returns = np.diff(x, prepend=0.0)
    else:
        log_returns = shocks.copy()

    if regime == "jumps":
        jumps = rng.random(n) < 1 / 5_000
        log_returns[jumps] += rng.choice([-1, 1], jumps.sum()) * rng.uniform(0.002, 0.006, jumps.sum())

    close = start_price * np.exp(np.cumsum(log_returns))

    # 時刻: 平日だけ。金曜の最後と月曜の最初の間に窓が開く（jumps のとき）
    business_days = pd.bdate_range("2025-01-06", periods=days)
    times = np.concatenate(
        [pd.date_range(day, periods=per_day, freq=f"{bar_minutes}min").to_numpy() for day in business_days]
    )
    gaps: list[int] = []
    if regime == "jumps":
        mondays = [k * per_day for k, day in enumerate(business_days) if day.weekday() == 0 and k > 0]
        for start in mondays:
            gap = rng.choice([-1, 1]) * rng.uniform(0.001, 0.004)
            close[start:] *= np.exp(gap)
            gaps.append(start)

    open_ = np.concatenate([[start_price], close[:-1]])
    # 窓開け: 月曜の始値そのものが飛ぶ（足の中で動くのではない）。
    # 逆指値が始値より不利な価格で約定することを、ここで試せるようにする
    for start in gaps:
        open_[start] = close[start] / np.exp(log_returns[start])
    wick = np.abs(rng.standard_normal((2, n))) * sigma * close * 0.5
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close},
        index=pd.DatetimeIndex(times, name="time"),
    )
