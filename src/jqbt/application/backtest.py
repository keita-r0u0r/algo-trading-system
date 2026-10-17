"""バックテストの実行（Imperative Shell）。

**判断は一切しない。** 約定するかどうかは ``core.execution``、
建玉と現金の更新は ``core.accounting``、成績の評価は ``core.metrics``。
ここにあるのは「日付を進めて、集めて、純粋関数に渡して、結果を溜める」手続きだけ。

## 1営業日の処理順序（この順序が唯一の重要事項）

    1. 前日に出した注文を、当日の **始値** で約定させる
    2. 当日の **終値** で評価し、記録を1点打つ
    3. 戦略に「当日まで」を渡して注文を受け取る
    4. その注文は翌日に持ち越す

T日の引けで判断し、T+1日の寄りで約定する。同じ日の終値で約定させると
「終値を見てから買った」ことになり、成績が劇的に良くなる。
最も入りやすく、最も気づきにくい先読み。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..core.accounting import apply_fill, snapshot
from ..core.contracts import (
    Bar,
    BacktestResult,
    Fill,
    Portfolio,
    RejectReason,
    Rejection,
    RoundTrip,
)
from ..core.costs import REALISTIC, CostModel
from ..core.execution import ExecutionContext, simulate
from .panel_index import PanelIndex, build_index, slice_index
from .ports import Strategy

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")

_EMPTY_ARRAY = np.empty(0)


# ----------------------------------------------------------------------
class PanelMarketView:
    """``MarketView`` の実装。``as_of`` で物理的に上限を切る。

    ここが先読み防止の要。戦略がどう書かれていても、
    このオブジェクトからは未来の行を取り出せない。

    上限の切り方は ``np.searchsorted`` による **位置**。
    マスクで毎回全行を比較していたときと意味論は同じで、計算量だけが違う。
    """

    __slots__ = ("_index", "_as_of", "_today_cache", "_lot_size")

    def __init__(self, index: PanelIndex, as_of: dt.date, lot_size: int = 100) -> None:
        self._index = index
        self._as_of = as_of
        self._today_cache: pd.DataFrame | None = None
        self._lot_size = lot_size

    @property
    def lot_size(self) -> int:
        """売買単位。戦略は数量をこの倍数に揃える。"""
        return self._lot_size

    @property
    def as_of(self) -> dt.date:
        return self._as_of

    def history(self, columns: list[str] | None = None) -> pd.DataFrame:
        """当日までの全データ。断面ではなく縦持ちのまま返す。

        戦略が横断的に見たい場合の逃げ道。毎日呼ぶと重いので、
        指標計算には ``values`` を使うこと。
        """
        frame = self._index.upto(self._as_of)
        return frame if columns is None else frame[columns]

    def values(
        self, symbol: str, column: str, lookback: int | None = None
    ) -> np.ndarray:
        """``as_of`` 以下の値を numpy 配列で返す（高速版）。

        ``series`` と同じ境界。日付索引を組み立てないぶんだけ速い。
        """
        entry = self._index.series.get(symbol)
        if entry is None:
            return _EMPTY_ARRAY
        return entry.upto(self._as_of, column, lookback)

    def series(
        self, symbol: str, column: str, lookback: int | None = None
    ) -> pd.Series:
        entry = self._index.series.get(symbol)
        if entry is None:
            return pd.Series(dtype=float)
        values = entry.upto(self._as_of, column, lookback)
        if values.size == 0:
            return pd.Series(dtype=float)
        return pd.Series(
            values, index=pd.DatetimeIndex(entry.index_upto(self._as_of, lookback))
        )

    def today(self, columns: list[str] | None = None) -> pd.DataFrame:
        if self._today_cache is None:
            self._today_cache = self._index.cross_section(self._as_of)
        frame = self._today_cache
        return frame if columns is None or frame.empty else frame[columns]

    def price(self, symbol: str) -> float | None:
        frame = self.today()
        if frame.empty or symbol not in frame.index:
            return None
        value = frame.at[symbol, "close"]
        return None if pd.isna(value) else float(value)

    def bar(self, symbol: str) -> Bar | None:
        frame = self.today()
        if frame.empty or symbol not in frame.index:
            return None
        return _to_bar(frame.loc[symbol])

    def symbols(self) -> list[str]:
        frame = self.today()
        return [] if frame.empty else [str(s) for s in frame.index]


def _to_bar(row) -> Bar:  # noqa: ANN001 - pandas Series
    def maybe(name: str) -> float | None:
        value = row.get(name)
        return None if value is None or pd.isna(value) else float(value)

    return Bar(
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume", 0.0) or 0.0),
        raw_close=maybe("raw_close"),
        turnover=maybe("turnover"),
        limit_up=bool(row.get("limit_up", False)),
        limit_down=bool(row.get("limit_down", False)),
    )


# ----------------------------------------------------------------------
@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 300_000.0
    costs: CostModel = REALISTIC
    #: 単元株（100株）を強制するか。
    #: False にすると理論上の優位性は測れるが、実行可能性は測れない
    enforce_lot_size: bool = True
    #: 売買単位。国内株は100株。1万円など少額なら単元未満株の1株単位で運用する
    lot_size: int = 100


class BacktestRunner:
    """パネル1枚に対してバックテストを回す。

    パネルを保持して使い回すのは、ウォークフォワードで同じデータに対して
    何十回も実行するため。毎回読み直すと時間の大半がI/Oになる。
    """

    def __init__(
        self,
        panel: pd.DataFrame | None = None,
        config: BacktestConfig | None = None,
        *,
        index: PanelIndex | None = None,
    ) -> None:
        if index is None:
            if panel is None:
                raise ValueError("panel か index のどちらかが必要です")
            missing = set(REQUIRED_COLUMNS) - set(panel.columns)
            if missing:
                raise ValueError(f"パネルに列が足りません: {sorted(missing)}")
            if not isinstance(panel.index, pd.MultiIndex):
                raise ValueError("パネルは (date, symbol) の MultiIndex が必要です")
            index = build_index(panel)

        self._index = index
        self.config = config or BacktestConfig()

    @property
    def panel(self) -> pd.DataFrame:
        return self._index.frame

    @property
    def dates(self) -> list[dt.date]:
        return list(self._index.dates)

    def slice(self, start: dt.date | None, end: dt.date | None) -> BacktestRunner:
        """期間を切った新しいランナー。ウォークフォワードの各窓で使う。

        助走（warmup）を含めたい場合は呼び出し側で start を早めに取ること。
        移動平均が引けるまで戦略は何もしないので、窓の頭は実質的に捨てられる。
        """
        return BacktestRunner(
            config=self.config, index=slice_index(self._index, start, end)
        )

    # ------------------------------------------------------------------
    def run(
        self,
        strategy: Strategy,
        label: str = "",
        params: dict[str, Any] | None = None,
    ) -> BacktestResult:
        portfolio = Portfolio(cash=self.config.initial_cash)
        pending: list = []

        snapshots: list = []
        fills: list[Fill] = []
        trips: list[RoundTrip] = []
        rejections: list[Rejection] = []

        for today in self._index.dates:
            view = PanelMarketView(self._index, today, self.config.lot_size)
            bars = view.today()
            if bars.empty:
                continue

            # --- 1. 前日の注文を当日の始値で約定 ---
            for order in pending:
                portfolio, fill, rejection, trip = self._execute(
                    order, today, view, portfolio
                )
                if fill is not None:
                    fills.append(fill)
                if trip is not None:
                    trips.append(trip)
                if rejection is not None:
                    rejections.append(rejection)
            pending = []

            # --- 2. 当日の終値で評価 ---
            prices = {
                str(symbol): float(value)
                for symbol, value in bars["close"].items()
                if not pd.isna(value)
            }
            snapshots.append(snapshot(portfolio, today, prices))

            # --- 3. 戦略に当日までを渡す ---
            pending = list(strategy.on_bar(view, portfolio) or [])

        return BacktestResult(
            initial_cash=self.config.initial_cash,
            snapshots=tuple(snapshots),
            fills=tuple(fills),
            round_trips=tuple(trips),
            rejections=tuple(rejections),
            label=label or getattr(strategy, "name", ""),
            params=dict(params or {}),
        )

    # ------------------------------------------------------------------
    def _execute(
        self, order, today, view, portfolio
    ) -> tuple[Portfolio, Fill | None, Rejection | None, RoundTrip | None]:
        """1件の注文を処理する。

        結果をタプルで返し、ランナー側に状態を持たせない。
        インスタンスに溜めると、同じランナーを使い回したときに
        前回の往復が混ざる（ウォークフォワードでは必ず使い回す）。
        """
        bar = view.bar(order.symbol)
        if bar is None:
            return (
                portfolio,
                None,
                Rejection(today, order.symbol, RejectReason.NO_QUOTE, order.quantity),
                None,
            )

        context = ExecutionContext(
            date=today,
            bar=bar,
            costs=self.config.costs,
            enforce_lot_size=self.config.enforce_lot_size,
            lot_size=self.config.lot_size,
        )
        outcome = simulate(order, context, portfolio)
        if isinstance(outcome, RejectReason):
            return (
                portfolio,
                None,
                Rejection(today, order.symbol, outcome, order.quantity),
                None,
            )

        portfolio, trip = apply_fill(portfolio, outcome)
        return portfolio, outcome, None, trip
