"""ウォークフォワード検証（Imperative Shell）。

窓ごとに「学習期間で最適化 → 検証期間で適用」を繰り返し、
**検証期間の結果だけ**を繋いで1本の成績にする。

    |--- 学習1 ---|-検証1-|
           |--- 学習2 ---|-検証2-|
                  |--- 学習3 ---|-検証3-|

    報告してよいのは 検証1 + 検証2 + 検証3 だけ

## 実装上の注意

**ウォームアップ。** 検証期間の初日から移動平均を引けるようにするには、
その手前のデータが要る。検証期間だけを切り出して回すと、窓の頭で
戦略が何もできず、成績が実態より悪く出る。``warmup`` はそのためにあり、
**学習期間側から借りてくるだけで、未来は一切参照しない。**

**資金のリセット。** 各窓は初期資金から始める。窓をまたいで建玉を
引き継がないのは、窓ごとの成績を独立に比較したいため。実運用の
複利効果を見たい場合は、検証期間のリターンを後から繋ぐ（``chained_equity``）。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..core.contracts import BacktestResult
from ..core.diagnostics import WalkForwardDiagnosis, diagnose
from ..core.metrics import Metrics, compute
from ..core.selection import Objective, Selection, sharpe_objective
from ..core.splitting import Window, validate_no_overlap
from .backtest import BacktestRunner
from .optimization import optimize
from .ports import StrategyFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowOutcome:
    """1つの窓の結果。"""

    window: Window
    selection: Selection
    #: 学習期間の成績。**報告してはいけない**（最良を選んだ後の数字）
    train_metrics: Metrics | None
    #: 検証期間の成績。報告してよいのはこちら
    test_metrics: Metrics | None
    test_result: BacktestResult | None
    error: str | None = None

    @property
    def params(self) -> dict[str, Any]:
        return self.selection.params if self.selection else {}


@dataclass(frozen=True)
class WalkForwardReport:
    outcomes: tuple[WindowOutcome, ...]
    diagnosis: WalkForwardDiagnosis
    objective_name: str
    #: 検証期間を繋いだ資産曲線（複利）
    chained_equity: pd.Series | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def test_metrics(self) -> list[Metrics]:
        return [o.test_metrics for o in self.outcomes if o.test_metrics]

    def selected_params(self) -> list[dict[str, Any]]:
        return [o.params for o in self.outcomes if o.params]


# ----------------------------------------------------------------------
class WalkForwardService:
    """ウォークフォワードの手続き。

    判断（窓の作り方・選択・診断）はすべて core の純粋関数が行う。
    ここにあるのは「窓を回して、集めて、渡す」だけ。
    """

    def __init__(
        self,
        runner: BacktestRunner,
        factory: StrategyFactory,
        grid: dict[str, list[Any]],
        objective: Objective = sharpe_objective,
        objective_name: str = "sharpe",
        warmup_days: int = 0,
        minimum_trades: int = 5,
    ) -> None:
        self._runner = runner
        self._factory = factory
        self._grid = grid
        self._objective = objective
        self._objective_name = objective_name
        self._warmup_days = warmup_days
        self._minimum_trades = minimum_trades

    # ------------------------------------------------------------------
    def execute(self, windows: tuple[Window, ...]) -> WalkForwardReport:
        if not windows:
            raise ValueError("窓が1つもありません。期間が短すぎる可能性があります")
        validate_no_overlap(windows)

        outcomes: list[WindowOutcome] = []
        for window in windows:
            outcomes.append(self._run_window(window))
            logger.info("walk-forward %s 完了", window.label())

        diagnosis = diagnose(
            train_scores=[self._score(o.train_metrics) for o in outcomes],
            test_scores=[self._score(o.test_metrics) for o in outcomes],
            selections=[o.params for o in outcomes if o.params],
        )

        notes = []
        failed = [o for o in outcomes if o.error]
        if failed:
            notes.append(
                f"{len(failed)}個の窓で最適化に失敗しました"
                f"（取引数が足りない等）: "
                + "、".join(f"#{o.window.index}" for o in failed)
            )

        return WalkForwardReport(
            outcomes=tuple(outcomes),
            diagnosis=diagnosis,
            objective_name=self._objective_name,
            chained_equity=self._chain(outcomes),
            notes=tuple(notes),
        )

    # ------------------------------------------------------------------
    def _run_window(self, window: Window) -> WindowOutcome:
        train_runner = self._runner.slice(window.train_start, window.train_end)
        try:
            optimization = optimize(
                train_runner,
                self._factory,
                self._grid,
                self._objective,
                minimum_trades=self._minimum_trades,
            )
        except ValueError as exc:
            return WindowOutcome(
                window=window,
                selection=None,  # type: ignore[arg-type]
                train_metrics=None,
                test_metrics=None,
                test_result=None,
                error=str(exc),
            )

        # --- 検証期間で適用する ---
        # ウォームアップは学習期間側から借りる。未来は参照しない
        test_start = self._with_warmup(window.test_start)
        test_runner = self._runner.slice(test_start, window.test_end)
        strategy = self._factory(optimization.params)
        test_result = test_runner.run(
            strategy, label=f"window-{window.index}", params=optimization.params
        )

        try:
            test_metrics = compute(test_result)
        except ValueError:
            test_metrics = None

        return WindowOutcome(
            window=window,
            selection=optimization.selection,
            train_metrics=optimization.best_metrics,
            test_metrics=test_metrics,
            test_result=test_result,
        )

    def _with_warmup(self, start: dt.date) -> dt.date:
        """検証開始日の手前に、指標を計算するための助走をつける。

        ここで借りるのは **過去のデータだけ**。検証期間より後ろには
        一切伸ばさないので、先読みにはならない。
        """
        if self._warmup_days <= 0:
            return start
        dates = self._runner.dates
        try:
            index = dates.index(start)
        except ValueError:
            return start
        return dates[max(index - self._warmup_days, 0)]

    def _score(self, metrics: Metrics | None) -> float | None:
        return None if metrics is None else self._objective(metrics)

    def _chain(self, outcomes: list[WindowOutcome]) -> pd.Series | None:
        """検証期間のリターンを繋いだ資産曲線。

        各窓は初期資金から始まるので、窓ごとのリターンを複利で繋ぎ直す。
        **助走期間は除く。** 含めると同じ日を複数の窓で二重に数えることになる。
        """
        segments: list[pd.Series] = []
        for outcome in outcomes:
            if outcome.test_result is None or not outcome.test_result.snapshots:
                continue
            snapshots = [
                s
                for s in outcome.test_result.snapshots
                if s.date >= outcome.window.test_start
            ]
            if len(snapshots) < 2:
                continue
            series = pd.Series(
                [s.equity for s in snapshots],
                index=pd.DatetimeIndex([s.date for s in snapshots]),
            )
            segments.append(series / series.iloc[0])

        if not segments:
            return None

        chained: list[pd.Series] = []
        level = 1.0
        for segment in segments:
            chained.append(segment * level)
            level = float(chained[-1].iloc[-1])
        return pd.concat(chained).sort_index()
