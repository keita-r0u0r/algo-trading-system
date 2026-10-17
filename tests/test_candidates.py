"""候補の戦略と、月次の物差し。

候補が「効くか」は実データで見る。ここで固定するのは、手で追える小さなケースでの
**動作の約束**（誰を選ぶか、いつ持たないか、月次の数え方）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from jqbt.application import BacktestConfig, BacktestRunner
from jqbt.application.backtest import PanelMarketView
from jqbt.application.panel_index import build_index
from jqbt.core.costs import CostModel
from jqbt.core.monthly import monthly_returns, monthly_summary
from jqbt.core.contracts import DailySnapshot
from jqbt.strategies import Breakout, Momentum, RegimeHold
from jqbt.strategies.candidates import RollingWindow

FREE = CostModel(cross_spread=False, impact_coefficient=0.0, max_participation=1.0)


def make_panel(paths: dict[str, list[float]]) -> pd.DataFrame:
    frames = []
    for symbol, closes in paths.items():
        days = pd.bdate_range("2024-01-01", periods=len(closes))
        frames.append(
            pd.DataFrame(
                {
                    "date": days, "symbol": symbol, "open": closes, "high": closes,
                    "low": closes, "close": closes, "raw_close": closes,
                    "volume": 1_000_000.0, "turnover": 1e9,
                }
            )
        )
    return pd.concat(frames).set_index(["date", "symbol"])


def run(strategy, paths, cash=10_000.0):
    runner = BacktestRunner(
        make_panel(paths), BacktestConfig(initial_cash=cash, costs=FREE, lot_size=1)
    )
    return runner.run(strategy)


def grow(start: float, rate: float, days: int) -> list[float]:
    return [start * (1 + rate) ** i for i in range(days)]


# ----------------------------------------------------------------------
def test_rolling_window_keeps_chronological_order_and_adds_symbols():
    panel = make_panel({"A": grow(100, 0.01, 8), "B": grow(200, 0.0, 8)})
    index = build_index(panel)
    window = RollingWindow(length=4)
    for day in index.dates:
        window.push(PanelMarketView(index, day, lot_size=1))

    closes = window.closes()
    assert closes.shape == (4, 2)
    a = window.symbols.index("A")
    assert list(closes[:, a]) == pytest.approx(grow(100, 0.01, 8)[-4:])
    assert window.ready


def test_momentum_buys_the_strongest_symbol():
    paths = {
        "UP": grow(100, 0.02, 30),
        "FLAT": grow(100, 0.0, 30),
        "DOWN": grow(100, -0.02, 30),
    }
    strategy = Momentum(lookback=10, skip=2, rebalance_every=5, max_positions=1, min_turnover=0)
    result = run(strategy, paths)

    bought = {f.symbol for f in result.fills if f.side.is_buy}
    assert bought == {"UP"}


def test_nothing_is_bought_before_the_warmup_is_filled():
    paths = {"UP": grow(100, 0.02, 12)}
    strategy = Momentum(lookback=20, skip=2, max_positions=1, min_turnover=0)
    assert not run(strategy, paths).fills


def test_regime_hold_stays_out_when_the_market_is_falling():
    falling = {s: grow(100, -0.01, 40) for s in ("A", "B", "C")}
    rising = {s: grow(100, 0.01, 40) for s in ("A", "B", "C")}

    out = run(RegimeHold(ma=10, rebalance_every=1, max_positions=2, min_turnover=0), falling)
    held = run(RegimeHold(ma=10, rebalance_every=1, max_positions=2, min_turnover=0), rising)

    assert not [f for f in out.fills if f.side.is_buy]
    assert [f for f in held.fills if f.side.is_buy]


def test_breakout_enters_on_a_new_high_and_exits_below_the_recent_low():
    # 横ばい → 上抜け → 急落
    path = [100.0] * 12 + [101, 104, 108, 112, 116] + [90.0, 85.0, 80.0]
    result = run(Breakout(entry=10, exit_=5, max_positions=1, min_turnover=0), {"X": path})

    sides = [f.side.value for f in result.fills]
    assert sides[:2] == ["buy", "sell"]


def test_strategies_never_buy_what_the_budget_cannot_cover():
    """1株の金額が1銘柄の予算を超える銘柄は、強くても買わない。"""
    paths = {"EXPENSIVE": grow(50_000, 0.02, 30), "CHEAP": grow(100, 0.0, 30)}
    strategy = Momentum(lookback=10, skip=2, rebalance_every=5, max_positions=1, min_turnover=0)
    result = run(strategy, paths, cash=10_000.0)

    assert all(f.symbol != "EXPENSIVE" for f in result.fills)


# ----------------------------------------------------------------------
def snapshots(values: list[tuple[str, float]]):
    return [
        DailySnapshot(date=dt.date.fromisoformat(d), equity=e, cash=e, invested=0.0, position_count=0)
        for d, e in values
    ]


def test_monthly_returns_use_calendar_month_ends():
    snaps = snapshots(
        [("2024-01-10", 100.0), ("2024-01-31", 110.0), ("2024-02-15", 99.0), ("2024-02-29", 121.0)]
    )
    returns = monthly_returns(snaps)

    assert list(returns.index) == ["2024-01", "2024-02"]
    assert returns["2024-01"] == pytest.approx(0.10)  # 初日の評価額が基準
    assert returns["2024-02"] == pytest.approx(0.10)  # 110 → 121


def test_monthly_summary_counts_loss_free_and_target_months():
    returns = pd.Series([0.05, -0.02, 0.0, 0.03], index=["a", "b", "c", "d"])
    summary = monthly_summary(returns, target=0.03)

    assert summary["months"] == 4
    assert summary["non_loss_ratio"] == pytest.approx(0.75)
    assert summary["reach_target_ratio"] == pytest.approx(0.5)
    assert summary["worst"] == pytest.approx(-0.02)


def test_monthly_summary_of_nothing_is_empty():
    assert monthly_summary(pd.Series(dtype=float)) == {"months": 0}
