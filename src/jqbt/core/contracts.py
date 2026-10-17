"""ドメインの値オブジェクト。

すべて不変（frozen）。状態を書き換えるのではなく、新しい値を返す。

不変にしておく理由は2つある。

  1. 純粋関数の引数として安全に渡せる。戦略に Portfolio を渡しても、
     戦略が中身を書き換えて会計を壊すことができない
  2. ウォークフォワードで同じ入力を何度も評価するとき、前の実行の
     残骸が混ざらない。再現性がタダで手に入る
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def is_buy(self) -> bool:
        return self is Side.BUY


class RejectReason(str, Enum):
    """約定しなかった理由。黙って捨てず、必ずこの語彙で残す。

    約定率を後から見られないと、「バックテストでは取れたが実際には
    取れない戦略」を見分けられない。
    """

    NO_QUOTE = "no_quote"
    NO_VOLUME = "no_volume"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"
    NO_POSITION = "no_position"
    INSUFFICIENT_CASH = "insufficient_cash"
    BELOW_LOT_SIZE = "below_lot_size"

    @property
    def label(self) -> str:
        return {
            RejectReason.NO_QUOTE: "その日に値がついていない",
            RejectReason.NO_VOLUME: "出来高が足りず約定しない",
            RejectReason.LIMIT_UP: "ストップ高のため買えない",
            RejectReason.LIMIT_DOWN: "ストップ安のため売れない",
            RejectReason.NO_POSITION: "保有していない",
            RejectReason.INSUFFICIENT_CASH: "資金不足",
            RejectReason.BELOW_LOT_SIZE: "単元株に満たない",
        }[self]


# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Bar:
    """1銘柄・1営業日の四本値。

    ``close`` は調整後。価格水準の判定に使う調整前は ``raw_close``。
    調整後は分割で遡及して書き換わるため、水準の条件に使うと先読みになる。
    """

    open: float
    high: float
    low: float
    close: float
    volume: float
    raw_close: float | None = None
    turnover: float | None = None
    limit_up: bool = False
    limit_down: bool = False

    @property
    def level_price(self) -> float:
        """価格水準の判定に使う値。調整前があればそちらを優先する。"""
        return self.raw_close if self.raw_close is not None else self.close


@dataclass(frozen=True, slots=True)
class Order:
    """翌営業日の寄りで執行する注文。

    価格は指定しない。指値にすると「約定したかどうか」の仮定が1つ増え、
    検証の信頼性がその分だけ落ちるため。
    """

    symbol: str
    side: Side
    quantity: float
    reason: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"数量は正の値が必要です: {self.quantity}")
        # 文字列で渡されても受け入れる。値オブジェクトの境界で正規化しておかないと、
        # 呼び出し側の表記ゆれが execution まで漏れてくる
        if not isinstance(self.side, Side):
            object.__setattr__(self, "side", Side(self.side))


@dataclass(frozen=True, slots=True)
class Fill:
    """約定1件（片道）。"""

    date: dt.date
    symbol: str
    side: Side
    quantity: float
    price: float
    commission: float
    #: 参照価格（約定日の始値）からのずれ。金額
    slippage: float
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    @property
    def cash_delta(self) -> float:
        """現金の増減。買いは負、売りは正。"""
        if self.side.is_buy:
            return -(self.notional + self.commission)
        return self.notional - self.commission


@dataclass(frozen=True, slots=True)
class Rejection:
    date: dt.date
    symbol: str
    reason: RejectReason
    requested_quantity: float = 0.0


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: float = 0.0
    average_price: float = 0.0
    opened_on: dt.date | None = None

    @property
    def is_open(self) -> bool:
        return self.quantity > 0

    @property
    def cost_basis(self) -> float:
        return self.average_price * self.quantity


@dataclass(frozen=True, slots=True)
class Portfolio:
    """現金と建玉。不変。"""

    cash: float
    positions: tuple[Position, ...] = ()

    def position(self, symbol: str) -> Position | None:
        return next((p for p in self.positions if p.symbol == symbol), None)

    def quantity(self, symbol: str) -> float:
        position = self.position(symbol)
        return position.quantity if position else 0.0

    @property
    def open_positions(self) -> tuple[Position, ...]:
        return tuple(p for p in self.positions if p.is_open)

    def market_value(self, prices: dict[str, float]) -> float:
        """建玉の評価額。値がつかない銘柄は取得単価で代用する。"""
        return sum(
            p.quantity * prices.get(p.symbol, p.average_price)
            for p in self.open_positions
        )

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def with_position(self, position: Position) -> Portfolio:
        """建玉を差し替えた新しいポートフォリオ。数量0なら取り除く。"""
        others = tuple(p for p in self.positions if p.symbol != position.symbol)
        if position.is_open:
            return replace(self, positions=others + (position,))
        return replace(self, positions=others)

    def with_cash(self, cash: float) -> Portfolio:
        return replace(self, cash=cash)


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """建玉を建ててから閉じるまで。期待値の分析はこの単位で行う。

    資産曲線は日次だが、優位性の統計的な検定は取引単位でしか意味を持たない。
    """

    symbol: str
    entry_date: dt.date
    exit_date: dt.date
    quantity: float
    entry_price: float
    exit_price: float
    pnl: float
    costs: float

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.entry_date).days

    @property
    def return_pct(self) -> float:
        basis = self.entry_price * self.quantity
        return self.pnl / basis if basis else 0.0


@dataclass(frozen=True, slots=True)
class DailySnapshot:
    """1営業日の記録。資産曲線と稼働率の元になる。"""

    date: dt.date
    equity: float
    cash: float
    invested: float
    position_count: int

    @property
    def exposure(self) -> float:
        return self.invested / self.equity if self.equity > 0 else 0.0


@dataclass(frozen=True)
class BacktestResult:
    """バックテスト1本の結果。これ自体は不変で、指標計算の入力になる。"""

    initial_cash: float
    snapshots: tuple[DailySnapshot, ...] = ()
    fills: tuple[Fill, ...] = ()
    round_trips: tuple[RoundTrip, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    label: str = ""
    params: dict = field(default_factory=dict)

    @property
    def final_equity(self) -> float:
        return self.snapshots[-1].equity if self.snapshots else self.initial_cash

    @property
    def start_date(self) -> dt.date | None:
        return self.snapshots[0].date if self.snapshots else None

    @property
    def end_date(self) -> dt.date | None:
        return self.snapshots[-1].date if self.snapshots else None

    @property
    def fill_rate(self) -> float | None:
        """注文のうち約定した割合。低いと実行可能性が疑わしい。"""
        attempted = len(self.fills) + len(self.rejections)
        return len(self.fills) / attempted if attempted else None
