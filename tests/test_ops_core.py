"""リスク制限・モード・増額の条件。資金が動く判断なので、境界を固定する。"""

from __future__ import annotations

import pytest

from jqbt.core.contracts import Order
from jqbt.core.gates import (
    GateThresholds,
    LiveStats,
    Mode,
    all_passed,
    can_switch,
    evaluate,
)
from jqbt.core.risk import RiskLimits, check_orders, is_loosening

LIMITS = RiskLimits(max_order_value=5_000, max_position_share=0.5, max_positions=2, max_daily_loss=0.05)


def check(orders, **kwargs):
    base = dict(prices={"A": 100.0, "B": 200.0, "C": 50.0}, holdings={}, equity=10_000.0,
                day_return=0.0, limits=LIMITS, lot_size=1)
    base.update(kwargs)
    return check_orders(orders, **base)


def test_a_buy_over_the_order_limit_is_clipped_not_dropped():
    result = check([Order("A", "buy", 100)])  # 10,000円ぶん要求 → 上限5,000円
    assert result.approved[0].quantity == 50
    assert result.clipped


def test_a_single_name_cannot_exceed_its_share_of_equity():
    result = check([Order("A", "buy", 40)], holdings={"A": 30.0})  # 保有3,000円 + 4,000円 > 5,000円
    assert result.approved[0].quantity == 20


def test_sells_always_pass_even_after_a_daily_loss_halt():
    result = check([Order("A", "sell", 10), Order("B", "buy", 5)], day_return=-0.06,
                   holdings={"A": 10.0})
    assert [o.symbol for o in result.approved] == ["A"]
    assert "日次損失" in result.rejected[0][1]


def test_position_count_limit_blocks_new_names_only():
    holdings = {"A": 10.0, "B": 5.0}
    result = check([Order("C", "buy", 10), Order("A", "buy", 1)], holdings=holdings)
    assert [o.symbol for o in result.approved] == ["A"]
    assert "同時保有" in result.rejected[0][1]


def test_a_buy_without_a_price_is_rejected():
    result = check([Order("ZZZ", "buy", 1)])
    assert not result.approved and result.rejected


def test_tightening_is_not_loosening_but_any_loosened_field_is():
    base = RiskLimits()
    assert not is_loosening(base, RiskLimits(max_order_value=1_000))
    assert is_loosening(base, RiskLimits(max_order_value=1_000, max_positions=9))
    assert not is_loosening(base, base)


def test_unimplemented_modes_cannot_be_entered():
    assert can_switch(Mode.STOP, Mode.SIGNAL)[0]
    assert can_switch(Mode.SIGNAL, Mode.STOP)[0]
    ok, reason = can_switch(Mode.SIGNAL, Mode.AUTO)
    assert not ok and "未実装" in reason


GOOD = LiveStats(days=90, fills=40, mean_slippage=0.004, assumed_slippage=0.004, max_drawdown=-0.08)


def test_all_gates_pass_for_a_healthy_run():
    assert all_passed(evaluate(GOOD))


@pytest.mark.parametrize(
    "change",
    [
        {"days": 30},
        {"fills": 5},
        {"mean_slippage": 0.01},
        {"mean_slippage": None},
        {"max_drawdown": -0.30},
        {"loss_breaches": 1},
    ],
)
def test_one_failed_gate_blocks_the_scale_up(change):
    stats = LiveStats(**{**GOOD.__dict__, **change})
    assert not all_passed(evaluate(stats, GateThresholds()))
