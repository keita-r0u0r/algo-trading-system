"""合成データ。J-Quants の認証情報がなくても回せるようにするため。

**この上で出た成績に意味はない。** 確かめられるのは
「エンジンが動くこと」と「先読みしていないこと」だけである。

ただし1点だけ意図的な仕掛けがある。``regime`` を "trend" にすると
トレンドの続きやすい系列を、"meanrev" にすると平均回帰する系列を作る。
移動平均クロスのような順張りが前者で勝ち後者で負けることを確認できれば、
**エンジンが売買の向きを正しく扱えている**ことの検算になる。
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..core.costs import tick_size


def generate_panel(
    symbols: list[str] | None = None,
    start: dt.date = dt.date(2019, 1, 1),
    end: dt.date = dt.date(2026, 6, 30),
    seed: int = 20260922,
    regime: str = "mixed",
    annual_drift: float = 0.03,
    annual_vol: float = 0.30,
) -> pd.DataFrame:
    """(date, symbol) の MultiIndex を持つパネルを作る。

    土日は除くが祝日は除かない。営業日カレンダーの正しさは
    実データで担保するもので、ここで真似ても意味がない。
    """
    rng = np.random.default_rng(seed)
    symbols = symbols or [f"T{i:04d}" for i in range(1, 31)]
    days = pd.bdate_range(start, end)
    n = len(days)

    rows = []
    for index, symbol in enumerate(symbols):
        mode = regime
        if regime == "mixed":
            mode = "trend" if index % 2 == 0 else "meanrev"

        base = float(rng.uniform(400, 3_000))
        daily_vol = annual_vol / np.sqrt(245)
        drift = annual_drift / 245

        shocks = rng.normal(drift, daily_vol, n)
        if mode == "trend":
            # 自己相関を入れて、動き出すと続きやすくする
            for i in range(1, n):
                shocks[i] += 0.15 * shocks[i - 1]
        elif mode == "meanrev":
            for i in range(1, n):
                shocks[i] -= 0.25 * shocks[i - 1]

        closes = base * np.exp(np.cumsum(shocks))
        # 呼値に丸める。丸めないと現実にありえない価格で約定してしまう
        closes = np.array([_round_price(c) for c in closes])

        intraday = np.abs(rng.normal(0, daily_vol * 0.6, n))
        opens = np.array(
            [_round_price(c * (1 + rng.normal(0, daily_vol * 0.4))) for c in closes]
        )
        highs = np.maximum(opens, closes) * (1 + intraday)
        lows = np.minimum(opens, closes) * (1 - intraday)

        # 出来高は値動きの大きさに相関させる
        moves = np.abs(np.diff(np.concatenate([[closes[0]], closes]))) / closes
        volume = rng.lognormal(11.5, 0.6, n) * (1 + 3 * moves)
        volume = np.round(volume / 100) * 100

        rows.append(
            pd.DataFrame(
                {
                    "date": days,
                    "symbol": symbol,
                    "open": opens,
                    "high": np.array([_round_price(h) for h in highs]),
                    "low": np.array([_round_price(l) for l in lows]),
                    "close": closes,
                    "volume": volume,
                    "raw_close": closes,
                    "turnover": closes * volume,
                    "limit_up": False,
                    "limit_down": False,
                    "regime": mode,
                }
            )
        )

    panel = pd.concat(rows, ignore_index=True)
    return panel.set_index(["date", "symbol"]).sort_index()


def _round_price(price: float) -> float:
    price = max(float(price), 1.0)
    tick = tick_size(price)
    return round(price / tick) * tick
