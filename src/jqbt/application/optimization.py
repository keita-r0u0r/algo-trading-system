"""パラメータ探索（Imperative Shell）。

格子を展開して総当たりする。**賢い最適化アルゴリズムは使わない。**

理由は速度ではなく、試行回数を数えられなくなるからです。ベイズ最適化や
遺伝的アルゴリズムは「実質的に何通り試したか」が曖昧になり、
選択バイアスの補正ができなくなります。総当たりなら N がそのまま分かる。

探索の賢さより、**何通り試したかを正直に数えること**のほうが
検証の正しさには効きます。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..core.contracts import BacktestResult
from ..core.metrics import Metrics, compute
from ..core.selection import Candidate, Objective, Selection, expand_grid, select_best
from .backtest import BacktestRunner
from .ports import StrategyFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OptimizationResult:
    selection: Selection
    candidates: tuple[Candidate, ...]
    #: 最良パラメータでの成績（学習期間のもの。報告してはいけない数字）
    best_metrics: Metrics | None
    best_result: BacktestResult | None

    @property
    def params(self) -> dict[str, Any]:
        return self.selection.params


def optimize(
    runner: BacktestRunner,
    factory: StrategyFactory,
    grid: dict[str, list[Any]],
    objective: Objective,
    minimum_trades: int = 5,
) -> OptimizationResult:
    """格子を総当たりし、最良のパラメータを選ぶ。

    ``minimum_trades`` に満たない候補は除外する。取引3件で Sharpe 5.0 のような
    「たまたま」を最良として選ばないため。ここを0にすると、ほぼ確実に
    取引数の極端に少ない候補が勝つ。
    """
    combinations = expand_grid(grid)
    candidates: list[Candidate] = []
    best_result: BacktestResult | None = None
    best_metrics: Metrics | None = None
    best_score = float("-inf")
    sample_sizes: list[int] = []

    for params in combinations:
        strategy = factory(params)
        result = runner.run(strategy, params=params)
        try:
            metrics = compute(result)
        except ValueError:
            continue  # 期間にデータがない

        if metrics.trade_count < minimum_trades:
            continue
        score = objective(metrics)
        if score is None:
            continue

        sample_sizes.append(metrics.trade_count)
        candidates.append(
            Candidate(
                params=params,
                score=float(score),
                detail={
                    "trades": metrics.trade_count,
                    "total_return": metrics.total_return,
                    "max_drawdown": metrics.max_drawdown,
                },
            )
        )
        if score > best_score:
            best_score, best_result, best_metrics = float(score), result, metrics

    if not candidates:
        raise ValueError(
            f"取引{minimum_trades}件以上の候補なし（{len(combinations)}通り中0）"
        )

    # 選択バイアスの補正には Sharpe 推定の標本数が要る。
    # 候補ごとに取引数が違うので、最も少ないもので保守的に見積もる
    sample_size = min(sample_sizes) if sample_sizes else 0
    selection = select_best(
        candidates, sample_size=sample_size, attempted=len(combinations)
    )

    return OptimizationResult(
        selection=selection,
        candidates=tuple(candidates),
        best_metrics=best_metrics,
        best_result=best_result,
    )
