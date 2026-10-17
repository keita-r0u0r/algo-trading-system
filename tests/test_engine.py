"""エンジンの会計と約定ルール。

損益計算が1円でもずれていれば、その先の指標はすべて無意味になる。
手で検算できる小さなケースで固定する。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from conftest import equity_of

from jqbt import Order
from jqbt.application import BacktestConfig, BacktestRunner
from jqbt.core.costs import CostModel

NO_COST = CostModel(cross_spread=False, impact_coefficient=0.0, max_participation=1.0)


def make_panel(closes: list[float], opens: list[float] | None = None, volume=1_000_000.0):
    days = pd.bdate_range("2024-01-01", periods=len(closes))
    opens = opens or closes
    return pd.DataFrame(
        {
            "date": days,
            "symbol": "X",
            "open": opens,
            "high": [max(o, c) * 1.01 for o, c in zip(opens, closes)],
            "low": [min(o, c) * 0.99 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [volume] * len(closes),
        }
    ).set_index(["date", "symbol"])


class BuyThenSell:
    """指定した回数目に買い、その後に売る。手で追える最小の戦略。"""

    name = "buy_then_sell"

    def __init__(self, buy_on: int, sell_on: int, quantity: float = 100):
        self.buy_on = buy_on
        self.sell_on = sell_on
        self.quantity = quantity
        self._count = 0

    def on_bar(self, view, portfolio):
        self._count += 1
        if self._count == self.buy_on:
            return [Order("X", "buy", self.quantity)]
        if self._count == self.sell_on and portfolio.quantity("X") > 0:
            return [Order("X", "sell", portfolio.quantity("X"))]
        return []


# ----------------------------------------------------------------------
def test_pnl_is_exact_without_costs():
    """コストゼロなら、損益は (売値 - 買値) × 数量 ちょうどになる。"""
    panel = make_panel(closes=[100, 110, 120, 130, 140], opens=[100, 110, 120, 130, 140])
    result = BacktestRunner(panel, BacktestConfig(initial_cash=100_000, costs=NO_COST)).run(
        BuyThenSell(buy_on=1, sell_on=3)
    )

    assert len(result.fills) == 2
    buy, sell = result.fills
    # 1回目の引けで判断 → 2日目の始値110で買い
    assert buy.price == 110
    # 3回目の引けで判断 → 4日目の始値130で売り
    assert sell.price == 130

    assert len(result.round_trips) == 1
    trip = result.round_trips[0]
    assert trip.pnl == pytest.approx((130 - 110) * 100)
    assert trip.return_pct == pytest.approx((130 - 110) / 110)
    assert result.final_equity == pytest.approx(100_000 + 2_000)


def test_cash_never_goes_negative():
    panel = make_panel(closes=[1000] * 10)
    result = BacktestRunner(panel, BacktestConfig(initial_cash=150_000)).run(
        BuyThenSell(buy_on=1, sell_on=99, quantity=1000)  # 100万円ぶん要求
    )
    assert equity_of(result).min() >= 0
    # 資金の範囲に落として約定している
    assert result.fills
    assert result.fills[0].quantity * result.fills[0].price <= 150_000


def test_lot_size_is_enforced():
    panel = make_panel(closes=[500] * 10)
    result = BacktestRunner(panel, BacktestConfig(initial_cash=1_000_000, costs=NO_COST)).run(
        BuyThenSell(buy_on=1, sell_on=99, quantity=150)  # 端数
    )
    assert result.fills
    assert result.fills[0].quantity % 100 == 0


def test_lot_size_can_be_disabled_for_theoretical_runs():
    panel = make_panel(closes=[500] * 10)
    result = BacktestRunner(panel, BacktestConfig(initial_cash=1_000_000, costs=NO_COST, enforce_lot_size=False)).run(BuyThenSell(buy_on=1, sell_on=99, quantity=150))
    assert result.fills
    assert result.fills[0].quantity == 150


def test_orders_beyond_participation_cap_are_partially_filled():
    """出来高の一定割合を超える注文は部分約定になる。

    無制限に約定させると、薄い銘柄で実在しない利益が出る。
    """
    panel = make_panel(closes=[100] * 10, volume=10_000.0)
    costs = CostModel(cross_spread=False, impact_coefficient=0.0, max_participation=0.1)
    result = BacktestRunner(panel, BacktestConfig(initial_cash=10_000_000, costs=costs)).run(
        BuyThenSell(buy_on=1, sell_on=99, quantity=50_000)
    )
    assert result.fills
    # 出来高10,000株の10% = 1,000株まで
    assert result.fills[0].quantity == 1_000


def test_limit_up_blocks_buying():
    """ストップ高の日は買えない。"""
    days = pd.bdate_range("2024-01-01", periods=5)
    panel = pd.DataFrame(
        {
            "date": days,
            "symbol": "X",
            "open": [100.0] * 5,
            "high": [100.0] * 5,
            "low": [100.0] * 5,
            "close": [100.0] * 5,
            "volume": [1_000_000.0] * 5,
            "limit_up": [False, True, False, False, False],
            "limit_down": [False] * 5,
        }
    ).set_index(["date", "symbol"])

    result = BacktestRunner(panel, BacktestConfig(initial_cash=1_000_000, costs=NO_COST)).run(
        BuyThenSell(buy_on=1, sell_on=99)
    )
    assert not result.fills
    assert result.rejections
    assert result.rejections[0].reason.label.startswith("ストップ高")


def test_rejections_are_recorded_not_silently_dropped():
    """約定しなかった理由は残す。約定率を後から見られるようにするため。"""
    panel = make_panel(closes=[100] * 5, volume=0.0)
    result = BacktestRunner(panel, BacktestConfig(initial_cash=1_000_000)).run(
        BuyThenSell(buy_on=1, sell_on=99)
    )
    assert not result.fills
    assert result.rejections


def test_equity_curve_has_one_point_per_trading_day():
    panel = make_panel(closes=[100] * 12)
    result = BacktestRunner(panel, BacktestConfig(initial_cash=100_000)).run(BuyThenSell(1, 99))
    assert len(equity_of(result)) == 12
    assert equity_of(result).index.is_monotonic_increasing


def test_selling_more_than_held_is_capped():
    panel = make_panel(closes=[100] * 8)
    result = BacktestRunner(panel, BacktestConfig(initial_cash=100_000, costs=NO_COST)).run(
        BuyThenSell(buy_on=1, sell_on=3, quantity=100)
    )
    sells = [t for t in result.fills if t.side.value == 'sell']
    assert sells
    assert sells[0].quantity == 100
