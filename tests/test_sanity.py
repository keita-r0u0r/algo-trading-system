"""エンジンの向きが正しいことの検算。

合成データには意図的に2種類の性質を仕込んである。

  trend   … 動き出すと続きやすい（順張りが効く）
  meanrev … 動いたら戻りやすい（順張りが効かない）

順張り戦略（移動平均クロス）が前者で後者より良い成績になるなら、
売買の向き・損益の符号・約定タイミングが揃って正しいと言える。

**これは戦略の有効性のテストではない。** 合成データ上の成績に
意味はない。確かめているのはエンジンの配線だけ。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jqbt import compute
from jqbt.application import BacktestConfig, BacktestRunner
from jqbt.core.costs import CostModel
from jqbt.strategies import SmaCross
from jqbt.infrastructure.synthetic import generate_panel

# 向きの検証にコストは邪魔なので落とす
NO_COST = CostModel(cross_spread=False, impact_coefficient=0.0, max_participation=1.0)


def _run(regime: str) -> float:
    panel = generate_panel(
        symbols=[f"S{i:02d}" for i in range(12)],
        start=dt.date(2019, 1, 1),
        end=dt.date(2026, 6, 30),
        regime=regime,
        seed=42,
    )
    result = BacktestRunner(
        panel,
        # 単元株の制約を外したいので資金は多めに
        BacktestConfig(initial_cash=5_000_000, costs=NO_COST),
    ).run(SmaCross(fast=20, slow=60, max_positions=5))
    return compute(result).total_return


def test_trend_following_does_better_on_trending_data():
    trending = _run("trend")
    mean_reverting = _run("meanrev")
    assert trending > mean_reverting, (
        f"順張りがトレンド系列({trending:.1%})で平均回帰系列({mean_reverting:.1%})に"
        "勝てていません。約定タイミングか損益の符号を疑ってください"
    )


def test_backtest_is_deterministic():
    """同じ入力なら完全に同じ結果になること。

    一致しないなら、どこかに現在時刻か乱数が紛れ込んでいる。
    """
    first = _run("trend")
    second = _run("trend")
    assert first == pytest.approx(second)


def test_costs_only_reduce_returns():
    """コストを重くして成績が良くなることはありえない。"""
    from jqbt.core.costs import OPTIMISTIC, PESSIMISTIC, REALISTIC

    panel = generate_panel(
        symbols=[f"S{i:02d}" for i in range(8)],
        start=dt.date(2021, 1, 1),
        end=dt.date(2026, 6, 30),
        regime="trend",
        seed=1,
    )
    results = []
    for model in (OPTIMISTIC, REALISTIC, PESSIMISTIC):
        outcome = BacktestRunner(panel, BacktestConfig(initial_cash=5_000_000, costs=model)).run(
            SmaCross(fast=20, slow=60, max_positions=4)
        )
        results.append(compute(outcome).total_return)

    assert results[0] >= results[1] >= results[2], (
        f"コストを重くしたのに成績が改善しています: {results}"
    )
