"""Port（契約）。

application はここに書かれた Protocol にだけ依存し、
infrastructure（Parquet、J-Quants、合成データ）を知らない。
依存の組み立ては interfaces 層だけが行う。

この分離の実利は3つある。

  1. テストで合成データを差し込める。ファイルも認証も要らない
  2. データ源を J-Quants から別のものに替えても、application は変わらない
  3. **先読みの境界が1箇所に集まる。** MarketView が as_of を守っていれば、
     戦略がどう書かれていても未来を見られない
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from ..core.contracts import Bar, Order, Portfolio


@runtime_checkable
class PanelSource(Protocol):
    """(date, symbol) の MultiIndex を持つパネルを供給する。"""

    def load(
        self,
        start: dt.date | None = None,
        end: dt.date | None = None,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame: ...


class MarketView(Protocol):
    """戦略に渡す「その日までの世界」。

    ``as_of`` より後の行には触れられない。規約ではなく実装がそうなっている。

    ``values`` は ``series`` の高速版で、numpy 配列をそのまま返す。
    戦略が毎日・全銘柄を舐める場合、pandas の Series を組み立てる費用が
    支配的になるため、指標計算だけが目的なら ``values`` を使う。
    """

    @property
    def as_of(self) -> dt.date: ...
    @property
    def lot_size(self) -> int: ...
    def values(
        self, symbol: str, column: str, lookback: int | None = None
    ) -> "np.ndarray": ...
    def series(self, symbol: str, column: str, lookback: int | None = None) -> pd.Series: ...
    def history(self, columns: list[str] | None = None) -> pd.DataFrame: ...
    def today(self, columns: list[str] | None = None) -> pd.DataFrame: ...
    def price(self, symbol: str) -> float | None: ...
    def bar(self, symbol: str) -> Bar | None: ...
    def symbols(self) -> list[str]: ...


class Strategy(Protocol):
    """売買ルール。純粋であることが期待される。

    現在時刻を見ない、乱数を使うなら seed を受け取る、DBを触らない。
    これが守られていれば、ウォークフォワードは何度回しても同じ結果になる。
    """

    name: str

    def on_bar(self, view: MarketView, portfolio: Portfolio) -> list[Order]: ...


#: パラメータからストラテジーを作る。最適化はこれを通じて戦略を差し替える
StrategyFactory = Callable[[dict[str, Any]], Strategy]


class LedgerPort(Protocol):
    """運用の記録（台帳）。**追記のみ**。消す・書き換えるための口は用意しない。

    設定の変更（``set_state``）は、旧値・新値・理由・時刻が監査ログに必ず残る。
    実装は ``infrastructure`` にあり、``application`` は SQLite を知らない。
    """

    def get_state(self, key: str, default: str | None = None) -> str | None: ...
    def set_state(self, key: str, value: str, *, reason: str = "", at: str = "") -> None: ...
    def audit_log(self, limit: int = 50) -> list[dict[str, Any]]: ...

    def add_signal(self, record: dict[str, Any]) -> int: ...
    def signals(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def set_signal_status(self, signal_id: int, status: str) -> None: ...

    def add_fill(self, record: dict[str, Any]) -> int: ...
    def fills(self) -> list[dict[str, Any]]: ...

    def save_equity(self, date: str, equity: float, cash: float) -> None: ...
    def equity_curve(self) -> list[tuple[str, float, float]]: ...


class SecretSource(Protocol):
    """秘密情報（APIキーなど）の取り出し口。

    実装は ``infrastructure/secrets.py``（GCP Secret Manager / .env）。
    **値をログやエラーメッセージに出さない。** 画面には有無だけを出す。
    """

    backend: str

    def get(self, name: str) -> str | None: ...
