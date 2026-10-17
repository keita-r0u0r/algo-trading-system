"""パネルの前処理（Imperative Shell）。

## なぜ必要か

素直に書くと ``MarketView`` は日ごとに「``as_of`` 以下の行」を毎回
マスクで切り出すことになる。これは1日あたり O(全行数) で、
1956営業日 × 5万行なら1回のバックテストで1億回の比較になる。
ウォークフォワードは同じデータに対してこれを100回以上走らせるので、
そのままでは現実的な時間で終わらない。

## やっていること

パネルを1度だけ走査して、2つの索引を作る。

  1. **日付 → 行の範囲**
     パネルは (date, symbol) でソート済みなので、同じ日付の行は連続している。
     開始・終了位置を覚えておけば、その日の断面は ``iloc[start:stop]`` で O(1)

  2. **銘柄 → 日付配列と値配列**
     ``np.searchsorted`` で「``as_of`` 以下」の位置を O(log n) で求められる。
     上限を **位置** で切るので、先読みの保証は maskを使う場合と変わらない

索引は不変で、ランナーが1度作って使い回す。**これは最適化であって、
意味論は一切変えていない。** 先読み防止のテストは索引を入れても通る。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SymbolSeries:
    """1銘柄ぶんの時系列。日付は昇順。"""

    dates: np.ndarray  # datetime64[ns]
    columns: dict[str, np.ndarray]

    def upto(self, as_of: dt.date, column: str, lookback: int | None) -> np.ndarray:
        """``as_of`` 以下の値を古い順に返す。

        ``side="right"`` なので ``as_of`` 当日は含み、翌日以降は含まない。
        ここが先読み防止の境界。
        """
        values = self.columns.get(column)
        if values is None:
            return np.empty(0)
        end = int(np.searchsorted(self.dates, np.datetime64(as_of, "ns"), side="right"))
        start = max(end - lookback, 0) if lookback else 0
        return values[start:end]

    def index_upto(self, as_of: dt.date, lookback: int | None) -> np.ndarray:
        end = int(np.searchsorted(self.dates, np.datetime64(as_of, "ns"), side="right"))
        start = max(end - lookback, 0) if lookback else 0
        return self.dates[start:end]


@dataclass(frozen=True)
class PanelIndex:
    """バックテストが繰り返し引くための索引。"""

    frame: pd.DataFrame  # (date, symbol) でソート済み
    dates: tuple[dt.date, ...]
    #: 日付 → frame 上の [start, stop)
    day_slices: dict[dt.date, tuple[int, int]]
    #: 銘柄 → 時系列
    series: dict[str, SymbolSeries]
    #: 数値列のみ（series に入れる対象）
    value_columns: tuple[str, ...]

    def upto(self, day: dt.date) -> pd.DataFrame:
        """その日までの全行。ソート済みなので位置で切れる。"""
        values = self.frame.index.get_level_values("date").values
        stop = int(np.searchsorted(values, np.datetime64(day, "ns"), side="right"))
        return self.frame.iloc[:stop]

    def cross_section(self, day: dt.date) -> pd.DataFrame:
        """その日の断面。銘柄がインデックス。"""
        bounds = self.day_slices.get(day)
        if bounds is None:
            return _EMPTY
        start, stop = bounds
        section = self.frame.iloc[start:stop]
        return section.droplevel("date")


_EMPTY = pd.DataFrame()


def build_index(panel: pd.DataFrame) -> PanelIndex:
    """パネルから索引を作る。1度だけ呼ぶ。"""
    frame = panel.sort_index()
    date_values = frame.index.get_level_values("date")

    # --- 日付 → 行範囲 ---
    unique_dates = date_values.unique()
    # ソート済みなので、各日付の開始位置は searchsorted で一括に取れる
    starts = np.searchsorted(date_values.values, unique_dates.values, side="left")
    stops = np.searchsorted(date_values.values, unique_dates.values, side="right")
    dates = tuple(pd.Timestamp(d).date() for d in unique_dates)
    day_slices = {
        day: (int(start), int(stop))
        for day, start, stop in zip(dates, starts, stops)
    }

    # --- 銘柄 → 時系列 ---
    value_columns = tuple(
        column
        for column in frame.columns
        if pd.api.types.is_numeric_dtype(frame[column])
    )
    series: dict[str, SymbolSeries] = {}
    for symbol, rows in frame.groupby(level="symbol", sort=False):
        symbol_dates = rows.index.get_level_values("date").values
        series[str(symbol)] = SymbolSeries(
            dates=symbol_dates,
            columns={c: rows[c].to_numpy(dtype=float, copy=False) for c in value_columns},
        )

    return PanelIndex(
        frame=frame,
        dates=dates,
        day_slices=day_slices,
        series=series,
        value_columns=value_columns,
    )


def slice_index(index: PanelIndex, start: dt.date | None, end: dt.date | None) -> PanelIndex:
    """期間を切った索引。ウォークフォワードの各窓で使う。

    索引を作り直すが、パネル全体の走査は1窓につき1回で済む。
    """
    if start is None and end is None:
        return index

    # 日付はソート済みなので、位置で切れば十分（マスクを作る必要がない）
    values = index.frame.index.get_level_values("date").values
    begin = (
        int(np.searchsorted(values, np.datetime64(start, "ns"), side="left"))
        if start is not None
        else 0
    )
    finish = (
        int(np.searchsorted(values, np.datetime64(end, "ns"), side="right"))
        if end is not None
        else len(values)
    )
    return build_index(index.frame.iloc[begin:finish])
