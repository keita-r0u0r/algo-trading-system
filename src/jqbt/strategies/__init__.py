"""売買ルール。純粋であることが期待される（core 扱い）。

現在時刻を見ない、乱数を使うなら seed を受け取る、外部I/Oを持たない。
守られていれば、ウォークフォワードを何度回しても同じ結果になる。

## 最適化に載せるには

``REGISTRY`` にファクトリとパラメータ格子を登録する。
格子の組み合わせ数がそのまま「試行回数 N」になり、選択バイアスの
大きさを決める。軸を1つ足すと N は掛け算で増えることを忘れないこと。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .base import Strategy, affordable_symbols, size_by_cash
from .baselines import BuyAndHold, RandomEntry
from .candidates import Breakout, LowVolatility, Momentum, RegimeHold, Reversal
from .sma_cross import SmaCross


@dataclass(frozen=True)
class StrategySpec:
    """戦略の登録情報。ファクトリと探索する格子。"""

    name: str
    factory: Callable[[dict[str, Any]], Strategy]
    grid: dict[str, list[Any]]
    #: 指標の計算に必要な助走日数。ウォークフォワードで使う
    warmup_days: int = 0

    @property
    def trial_count(self) -> int:
        total = 1
        for values in self.grid.values():
            total *= len(values)
        return total


def _sma_factory(params: dict[str, Any]) -> Strategy:
    return SmaCross(
        fast=int(params.get("fast", 25)),
        slow=int(params.get("slow", 75)),
        max_positions=int(params.get("max_positions", 3)),
    )


def _hold_factory(params: dict[str, Any]) -> Strategy:
    return BuyAndHold(max_positions=int(params.get("max_positions", 3)))


def _random_factory(params: dict[str, Any]) -> Strategy:
    return RandomEntry(
        holding_days=int(params.get("holding_days", 20)),
        max_positions=int(params.get("max_positions", 3)),
        seed=int(params.get("seed", 0)),
    )


def _momentum_factory(params: dict[str, Any]) -> Strategy:
    return Momentum(
        lookback=int(params.get("lookback", 120)),
        skip=int(params.get("skip", 20)),
        max_positions=int(params.get("max_positions", 5)),
    )


def _reversal_factory(params: dict[str, Any]) -> Strategy:
    return Reversal(
        days=int(params.get("days", 5)),
        hold=int(params.get("hold", 5)),
        max_positions=int(params.get("max_positions", 5)),
    )


def _lowvol_factory(params: dict[str, Any]) -> Strategy:
    return LowVolatility(
        lookback=int(params.get("lookback", 60)),
        max_positions=int(params.get("max_positions", 5)),
    )


def _breakout_factory(params: dict[str, Any]) -> Strategy:
    return Breakout(
        entry=int(params.get("entry", 55)),
        max_positions=int(params.get("max_positions", 5)),
    )


def _regime_factory(params: dict[str, Any]) -> Strategy:
    return RegimeHold(
        ma=int(params.get("ma", 50)),
        max_positions=int(params.get("max_positions", 5)),
    )


REGISTRY: dict[str, StrategySpec] = {
    "sma_cross": StrategySpec(
        name="sma_cross",
        factory=_sma_factory,
        # 3 x 3 = 9通り。これでも「9回引いた中の最良」のゲタは乗る
        grid={"fast": [10, 25, 40], "slow": [50, 75, 100]},
        warmup_days=120,
    ),
    # 以下は候補。格子は小さく固定してある。軸を足すと試行回数 N が掛け算で増える
    "momentum": StrategySpec(
        name="momentum",
        factory=_momentum_factory,
        grid={"lookback": [60, 120], "skip": [5, 20]},
        warmup_days=121,
    ),
    "reversal": StrategySpec(
        name="reversal",
        factory=_reversal_factory,
        grid={"days": [3, 5], "hold": [5, 10]},
        warmup_days=21,
    ),
    "lowvol": StrategySpec(
        name="lowvol",
        factory=_lowvol_factory,
        grid={"lookback": [60, 120]},
        warmup_days=121,
    ),
    "breakout": StrategySpec(
        name="breakout",
        factory=_breakout_factory,
        grid={"entry": [55, 100]},
        warmup_days=101,
    ),
    "regime_hold": StrategySpec(
        name="regime_hold",
        factory=_regime_factory,
        grid={"ma": [0, 50, 100]},
        warmup_days=101,
    ),
    "buy_and_hold": StrategySpec(
        name="buy_and_hold", factory=_hold_factory, grid={}, warmup_days=0
    ),
    "random": StrategySpec(
        name="random",
        factory=_random_factory,
        grid={"holding_days": [10, 20, 40]},
        warmup_days=0,
    ),
}

__all__ = [
    "Strategy",
    "StrategySpec",
    "REGISTRY",
    "size_by_cash",
    "affordable_symbols",
    "BuyAndHold",
    "RandomEntry",
    "SmaCross",
    "Momentum",
    "Reversal",
    "LowVolatility",
    "Breakout",
    "RegimeHold",
]
