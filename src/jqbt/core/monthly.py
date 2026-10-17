"""月次の物差し（純粋関数）。

「資産を増やす目標を達成する確率」を、1か月単位で見るための指標。
1取引の勝率ではなく、**月ごとの資産の増減**で数える。

注意: 観測できる月数は少ない（2年なら約24か月）。月次の割合は
「傾向」であって、統計的な証拠にはならない。月数も必ず併記する。
"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd


def monthly_returns(snapshots: Sequence) -> pd.Series:
    """月末の評価額から、暦月ごとのリターンを出す。

    最初の月は、最初の日の評価額を基準にする（月初の評価額が無いため）。
    月の途中から始まった月も1か月として数えるので、月数は端数を含む。
    """
    if not snapshots:
        return pd.Series(dtype=float)
    equity = pd.Series(
        [s.equity for s in snapshots],
        index=pd.DatetimeIndex([s.date for s in snapshots]),
        dtype=float,
    )
    month_end = equity.groupby(equity.index.to_period("M")).last()
    previous = month_end.shift(1)
    previous.iloc[0] = equity.iloc[0]
    returns = month_end / previous - 1.0
    returns.index = returns.index.astype(str)
    return returns


def monthly_summary(returns: pd.Series, target: float = 0.02) -> dict[str, Any]:
    """月次リターンの要約。

    ``target`` は月次の目標リターン。``reach_target_ratio`` は、それ以上だった月の割合。
    """
    n = int(len(returns))
    if n == 0:
        return {"months": 0}
    values = returns.to_numpy(dtype=float)
    return {
        "months": n,
        "mean": float(values.mean()),
        "median": float(pd.Series(values).median()),
        "worst": float(values.min()),
        "best": float(values.max()),
        "non_loss_ratio": float((values >= 0).mean()),
        "reach_target_ratio": float((values >= target).mean()),
        "target": target,
    }
