"""Imperative Shell — 手続き。

「集める → 純粋関数に渡す → 結果を溜める」の3段だけを書く。
判断は core に置く。
"""

from .backtest import BacktestConfig, BacktestRunner, PanelMarketView
from .optimization import OptimizationResult, optimize
from .walkforward import WalkForwardReport, WalkForwardService, WindowOutcome

__all__ = [
    "BacktestRunner",
    "BacktestConfig",
    "PanelMarketView",
    "optimize",
    "OptimizationResult",
    "WalkForwardService",
    "WalkForwardReport",
    "WindowOutcome",
]
