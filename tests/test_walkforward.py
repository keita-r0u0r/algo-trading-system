"""ウォークフォワードの統合テスト。

最適化と検証が正しく **分離されている** ことを確かめる。
ここが崩れると、学習期間の成績を検証結果として報告してしまう。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jqbt.application import BacktestConfig, BacktestRunner
from jqbt.application.optimization import optimize
from jqbt.application.walkforward import WalkForwardService
from jqbt.core.costs import CostModel
from jqbt.core.selection import sharpe_objective
from jqbt.core.splitting import walk_forward
from jqbt.infrastructure.synthetic import generate_panel
from jqbt.strategies import REGISTRY

NO_COST = CostModel(cross_spread=False, impact_coefficient=0.0, max_participation=1.0)


@pytest.fixture(scope="module")
def runner():
    panel = generate_panel(
        symbols=[f"S{i:02d}" for i in range(6)],
        start=dt.date(2020, 1, 1),
        end=dt.date(2025, 6, 30),
        regime="trend",
        seed=11,
    )
    return BacktestRunner(
        panel, BacktestConfig(initial_cash=5_000_000, costs=NO_COST)
    )


@pytest.fixture(scope="module")
def spec():
    return REGISTRY["sma_cross"]


# ----------------------------------------------------------------------
def test_optimization_reports_how_many_were_tried(runner, spec):
    """試行回数を失わないこと。

    失うと、あとから多重検定の補正ができなくなる。
    """
    result = optimize(runner, spec.factory, spec.grid, sharpe_objective, minimum_trades=3)

    assert result.selection.attempted == spec.trial_count
    assert 1 <= result.selection.trials <= spec.trial_count
    assert result.params in [dict(c.params) for c in result.candidates]


def test_optimization_excludes_candidates_with_too_few_trades(runner, spec):
    """取引数の少ない「たまたま」を最良として選ばないこと。"""
    lenient = optimize(runner, spec.factory, spec.grid, sharpe_objective, minimum_trades=1)
    strict = optimize(runner, spec.factory, spec.grid, sharpe_objective, minimum_trades=20)

    assert strict.selection.trials <= lenient.selection.trials
    for candidate in strict.candidates:
        assert candidate.detail["trades"] >= 20


# ----------------------------------------------------------------------
def test_walkforward_uses_training_data_only_for_selection(runner, spec):
    """検証期間の成績が、パラメータ選択に影響していないこと。

    学習側だけで選んでいれば、検証期間を差し替えても
    選ばれるパラメータは変わらないはず。
    """
    dates = runner.dates
    windows = walk_forward(dates, train_size=250, test_size=120)
    assert windows

    service = WalkForwardService(
        runner, spec.factory, spec.grid, sharpe_objective,
        warmup_days=spec.warmup_days, minimum_trades=3,
    )
    report = service.execute(windows[:3])

    for outcome in report.outcomes:
        if outcome.error:
            continue
        # 同じ学習期間を単独で最適化しても、同じパラメータが選ばれる
        train_runner = runner.slice(
            outcome.window.train_start, outcome.window.train_end
        )
        standalone = optimize(
            train_runner, spec.factory, spec.grid, sharpe_objective, minimum_trades=3
        )
        assert standalone.params == outcome.params


def test_walkforward_is_reproducible(runner, spec):
    windows = walk_forward(runner.dates, train_size=250, test_size=120)[:3]
    service = WalkForwardService(
        runner, spec.factory, spec.grid, sharpe_objective,
        warmup_days=spec.warmup_days, minimum_trades=3,
    )

    first = service.execute(windows)
    second = service.execute(windows)

    assert [o.params for o in first.outcomes] == [o.params for o in second.outcomes]
    assert first.diagnosis.mean_test_score == second.diagnosis.mean_test_score


def test_chained_equity_covers_only_test_periods(runner, spec):
    """繋いだ資産曲線に、助走期間が混ざっていないこと。

    混ざると同じ日を複数の窓で二重に数えることになる。
    """
    windows = walk_forward(runner.dates, train_size=250, test_size=120)[:4]
    service = WalkForwardService(
        runner, spec.factory, spec.grid, sharpe_objective,
        warmup_days=spec.warmup_days, minimum_trades=3,
    )
    report = service.execute(windows)

    if report.chained_equity is None:
        pytest.skip("この期間では有効な窓がありません")

    curve = report.chained_equity
    assert curve.index.is_monotonic_increasing
    #: 曲線の開始は最初の窓の検証開始以降（学習期間には食い込まない）
    assert curve.index[0].date() >= windows[0].test_start
    assert curve.index[-1].date() <= windows[-1].test_end
    #: 日付の重複がない
    assert not curve.index.duplicated().any()


def test_warmup_does_not_extend_into_the_future(runner, spec):
    """助走は過去にだけ伸ばすこと。

    未来側に伸ばしたら、それは先読みそのもの。
    """
    service = WalkForwardService(
        runner, spec.factory, spec.grid, sharpe_objective,
        warmup_days=100, minimum_trades=3,
    )
    windows = walk_forward(runner.dates, train_size=250, test_size=120)
    window = windows[2]

    warmed = service._with_warmup(window.test_start)
    assert warmed <= window.test_start


def test_windows_without_enough_trades_are_reported_not_hidden(runner, spec):
    """最適化できなかった窓を黙って捨てないこと。

    捨てると「うまくいった窓だけ」の成績になる。
    """
    service = WalkForwardService(
        runner, spec.factory, spec.grid, sharpe_objective,
        warmup_days=spec.warmup_days,
        minimum_trades=10_000,  # わざと全部落とす
    )
    windows = walk_forward(runner.dates, train_size=250, test_size=120)[:3]
    report = service.execute(windows)

    assert all(o.error for o in report.outcomes)
    assert report.notes
    assert "最適化に失敗" in report.notes[0]
