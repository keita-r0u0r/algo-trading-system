"""合成ルート（Composition Root）。

依存の組み立てはこのファイルだけが行う。application は
infrastructure を知らないので、繋ぐ場所が1箇所に集まる。

データ源の切り替え（Parquet / 合成データ）もここ。
新しい提供元を足すときに触るのもここだけで済む。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

import pandas as pd

from ..application.backtest import BacktestConfig, BacktestRunner
from ..application.walkforward import WalkForwardService
from ..core.costs import OPTIMISTIC, PESSIMISTIC, REALISTIC, CostModel
from ..core.selection import OBJECTIVES, Objective
from ..strategies import REGISTRY, StrategySpec

COST_MODELS: dict[str, CostModel] = {
    "optimistic": OPTIMISTIC,
    "realistic": REALISTIC,
    "pessimistic": PESSIMISTIC,
}


@dataclass(frozen=True)
class DataRequest:
    synthetic: bool = False
    start: dt.date | None = None
    end: dt.date | None = None
    symbols: list[str] | None = None
    regime: str = "mixed"
    seed: int = 20260922


def load_panel(request: DataRequest) -> pd.DataFrame:
    """データ源を選んでパネルを返す。

    合成データは認証なしで動くが、**成績に意味はない**。
    仕組みの確認と、先読みしていないことの検証にだけ使う。
    """
    if request.synthetic:
        from ..infrastructure.synthetic import generate_panel

        return generate_panel(
            symbols=request.symbols,
            start=request.start or dt.date(2019, 1, 1),
            end=request.end or dt.date(2026, 6, 30),
            regime=request.regime,
            seed=request.seed,
        )

    from ..infrastructure.parquet_store import load_panel as load

    return load(request.start, request.end, request.symbols)


#: 単元未満株の追加スプレッド（片道）の既定。**証券会社の実測値に置き換えること。**
#: 実測がないので、日足の始値で約定できるとは見なさず、やや悲観側に置いている
ODD_LOT_EXTRA_SPREAD = 0.003


def default_extra_spread(lot_size: int) -> float:
    """単元未満株（売買単位が100株未満）のときだけ追加スプレッドを見込む。"""
    return ODD_LOT_EXTRA_SPREAD if lot_size < 100 else 0.0


def build_runner(
    panel: pd.DataFrame,
    cash: float = 300_000.0,
    costs: str | CostModel = "realistic",
    enforce_lot_size: bool = True,
    lot_size: int = 100,
    extra_spread: float | None = None,
) -> BacktestRunner:
    """``extra_spread`` を省略すると、``lot_size`` に応じた既定値を使う。"""
    model = COST_MODELS[costs] if isinstance(costs, str) else costs
    spread = default_extra_spread(lot_size) if extra_spread is None else extra_spread
    if spread:
        model = replace(model, extra_spread_rate=spread)
    return BacktestRunner(
        panel,
        BacktestConfig(
            initial_cash=cash,
            costs=model,
            enforce_lot_size=enforce_lot_size,
            lot_size=lot_size,
        ),
    )


def resolve_strategy(name: str) -> StrategySpec:
    if name not in REGISTRY:
        raise SystemExit(
            f"未知の戦略: {name}（利用できるのは {', '.join(sorted(REGISTRY))}）"
        )
    return REGISTRY[name]


def resolve_objective(name: str) -> tuple[Objective, str]:
    if name not in OBJECTIVES:
        raise SystemExit(
            f"未知の目的関数: {name}（利用できるのは {', '.join(sorted(OBJECTIVES))}）"
        )
    return OBJECTIVES[name], name


def build_walkforward(
    runner: BacktestRunner,
    spec: StrategySpec,
    objective_name: str = "sharpe",
    minimum_trades: int = 10,
) -> WalkForwardService:
    objective, name = resolve_objective(objective_name)
    return WalkForwardService(
        runner=runner,
        factory=spec.factory,
        grid=spec.grid,
        objective=objective,
        objective_name=name,
        warmup_days=spec.warmup_days,
        minimum_trades=minimum_trades,
    )
