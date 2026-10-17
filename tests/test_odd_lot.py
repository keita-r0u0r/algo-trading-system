"""単元未満株（1株単位）で回せること。

1万円のような少額では、100株単位だと母集団のほとんどが買えない。
売買単位を設定値にしたとき、約定・コスト・戦略の配分が一貫して
その単位に従うことを、手で検算できる小さなケースで固定する。
"""

from __future__ import annotations

import pandas as pd
import pytest

from test_engine import NO_COST, BuyThenSell, make_panel

from jqbt.application import BacktestConfig, BacktestRunner
from jqbt.core.costs import CostModel
from jqbt.infrastructure.parquet_store import capital_feasibility
from jqbt.interfaces.composition import (
    ODD_LOT_EXTRA_SPREAD,
    build_runner,
    default_extra_spread,
)
from jqbt.strategies import BuyAndHold


def test_one_share_lot_allows_small_orders():
    """100株単位では買えない1万円でも、1株単位なら買える。"""
    panel = make_panel(closes=[1_500] * 10)

    at_100 = BacktestRunner(panel, BacktestConfig(initial_cash=10_000, costs=NO_COST)).run(
        BuyThenSell(buy_on=1, sell_on=99, quantity=100)
    )
    at_1 = BacktestRunner(
        panel, BacktestConfig(initial_cash=10_000, costs=NO_COST, lot_size=1)
    ).run(BuyThenSell(buy_on=1, sell_on=99, quantity=100))

    assert not at_100.fills
    assert at_1.fills
    # 10,000円 ÷ 1,500円 = 6株まで
    assert at_1.fills[0].quantity == 6


def test_quantity_is_floored_to_the_configured_lot():
    panel = make_panel(closes=[100] * 10)
    result = BacktestRunner(
        panel, BacktestConfig(initial_cash=10_000_000, costs=NO_COST, lot_size=10)
    ).run(BuyThenSell(buy_on=1, sell_on=99, quantity=57))
    assert result.fills[0].quantity == 50


def test_extra_spread_moves_the_price_against_us():
    """買いは高く、売りは安く約定する。有利側にずらすとバックテストが楽観的になる。"""
    costs = CostModel(
        cross_spread=False, impact_coefficient=0.0, max_participation=1.0,
        extra_spread_rate=0.01,
    )
    panel = make_panel(closes=[1_000] * 10)
    result = BacktestRunner(
        panel, BacktestConfig(initial_cash=100_000, costs=costs, lot_size=1)
    ).run(BuyThenSell(buy_on=1, sell_on=3, quantity=10))

    buy, sell = result.fills
    assert buy.price == pytest.approx(1_010)
    assert sell.price == pytest.approx(990)
    assert result.round_trips[0].pnl == pytest.approx((990 - 1_010) * 10)


def test_default_extra_spread_applies_only_below_a_full_lot():
    assert default_extra_spread(100) == 0.0
    assert default_extra_spread(1) == ODD_LOT_EXTRA_SPREAD


def test_build_runner_adds_the_spread_for_odd_lots_only():
    panel = make_panel(closes=[1_000] * 5)
    assert build_runner(panel, 10_000, "optimistic", True, 1).config.costs.extra_spread_rate > 0
    assert build_runner(panel, 10_000, "optimistic", True, 100).config.costs.extra_spread_rate == 0
    # 明示した値は既定を上書きする
    assert build_runner(panel, 10_000, "optimistic", True, 1, 0.0).config.costs.extra_spread_rate == 0


def test_strategy_sizes_positions_by_the_view_lot_size():
    """戦略が100株を決め打ちしていると、1株単位にしても買えないまま終わる。"""
    days = pd.bdate_range("2024-01-01", periods=5)
    panel = pd.concat(
        [
            pd.DataFrame(
                {
                    "date": days, "symbol": symbol, "open": price, "high": price,
                    "low": price, "close": price, "volume": 1_000_000.0,
                }
            )
            for symbol, price in (("A", 1_000.0), ("B", 2_000.0))
        ]
    ).set_index(["date", "symbol"])

    result = BacktestRunner(
        panel, BacktestConfig(initial_cash=10_000, costs=NO_COST, lot_size=1)
    ).run(BuyAndHold(max_positions=2))

    bought = {fill.symbol: fill.quantity for fill in result.fills}
    # 1銘柄あたり5,000円 → Aは5株、Bは2株
    assert bought == {"A": 5, "B": 2}


def test_feasibility_follows_the_lot_size():
    days = pd.bdate_range("2024-01-01", periods=2)
    prices = [500.0, 2_000.0, 8_000.0, 20_000.0]
    panel = pd.concat(
        [
            pd.DataFrame(
                {"date": days, "symbol": f"S{i}", "close": price, "raw_close": price}
            )
            for i, price in enumerate(prices)
        ]
    ).set_index(["date", "symbol"])

    at_100 = capital_feasibility(panel, 10_000, 1, lot_size=100)
    at_1 = capital_feasibility(panel, 10_000, 1, lot_size=1)

    assert at_100["affordable_count"] == 0
    assert at_1["affordable_count"] == 3  # 500, 2,000, 8,000円は買える
    assert at_1["max_affordable_price"] == 10_000


def test_affordability_uses_the_unadjusted_price():
    """調整済みの価格で買えるか判定すると、将来の分割を知っているのと同じ偏りが出る。

    調整後の終値は100円でも、当時の実際の価格が1,500円なら、予算1,000円では買えない。
    """
    from jqbt.application import BacktestConfig
    from jqbt.application.backtest import PanelMarketView
    from jqbt.application.panel_index import build_index
    from jqbt.strategies import affordable_symbols

    days = pd.bdate_range("2024-01-01", periods=2)
    panel = pd.DataFrame(
        {
            "date": days.repeat(1),
            "symbol": "S",
            "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
            "raw_close": 1_500.0, "volume": 1_000_000.0,
        }
    ).set_index(["date", "symbol"])

    view = PanelMarketView(build_index(panel), days[-1].date(), lot_size=1)

    assert affordable_symbols(view, ["S"], 1_000.0) == []
    assert affordable_symbols(view, ["S"], 2_000.0) == ["S"]
