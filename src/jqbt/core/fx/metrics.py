"""FX の成績の要約（純粋関数）。"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from .model import FxTrade


def summarize(
    initial: float, equity: np.ndarray, days: Sequence[Any], trades: Sequence[FxTrade], losscut: bool
) -> dict[str, Any]:
    """取引と資産曲線から、成績を出す。``days`` は資産曲線の各点の日付。"""
    final = float(equity[-1]) if len(equity) else initial
    peak = np.maximum.accumulate(equity) if len(equity) else np.array([initial])
    drawdown = float(np.min(equity / peak - 1)) if len(equity) else 0.0

    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    n = len(pnls)
    mean = float(pnls.mean()) if n else None
    std = float(pnls.std(ddof=1)) if n > 1 else None
    t_stat = mean / (std / math.sqrt(n)) if n > 1 and std else None

    # 日次の Sharpe（日をまたいだ資産曲線の終値で）
    daily = None
    if len(equity):
        last_by_day: dict[Any, float] = {}
        for day, value in zip(days, equity):
            last_by_day[day] = float(value)
        series = np.array(list(last_by_day.values()))
        if len(series) > 2:
            returns = np.diff(series) / series[:-1]
            if returns.std() > 0:
                daily = float(returns.mean() / returns.std())

    return {
        "initial": initial,
        "final": final,
        "total_return": final / initial - 1 if initial else 0.0,
        "max_drawdown": drawdown,
        "trades": n,
        "win_rate": float(len(wins) / n) if n else None,
        "expectancy": mean,
        "t_stat": t_stat,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() else None,
        "total_cost": float(sum(t.cost for t in trades)),
        "sharpe_daily": daily,
        "losscut": losscut,
        "negative_balance": final < 0,
    }
