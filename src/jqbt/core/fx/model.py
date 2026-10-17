"""FX の銘柄仕様・コスト・建玉の会計・損切り/利確・ロスカット（純粋関数）。

## 価格の約束

足の価格は **仲値**（買値と売値の中間）。買いは仲値 + 半スプレッド（買値）、
売りは仲値 − 半スプレッド（売値）で約定する。そのうえに、成行の不利なずれを足す。

## 足の中で何が先に起きたかは分からない

1本の足の中で、損切りと利確の両方の価格に届いた場合、**損切りが先に起きたと見なす**。
有利な側を先にすると、バックテストが現実より良く出る。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class FxInstrument:
    symbol: str = "USDJPY"
    #: 1 pip の値幅（円）
    pip: float = 0.01
    #: 最小の取引単位（通貨）。業者により1〜1,000通貨。**要確認**
    min_units: int = 1_000
    #: レバレッジの上限。国内の個人は25倍
    leverage: float = 25.0
    #: 証拠金維持率（有効証拠金 ÷ 必要証拠金）がこれ以下でロスカット。業者により50〜100%。**要確認**
    losscut_ratio: float = 0.5

    def required_margin(self, units: float, price: float) -> float:
        return abs(units) * price / self.leverage

    def round_units(self, units: float) -> float:
        """取引単位の倍数に切り捨てる（符号は保つ）。"""
        lots = int(abs(units) / self.min_units)
        return float(lots * self.min_units) * (1 if units >= 0 else -1)


@dataclass(frozen=True)
class FxCosts:
    #: スプレッド（pips）。業者・時間帯で変わる。早朝や指標発表時は広がる
    spread_pips: float = 0.2
    #: 成行の不利なずれ（pips）
    slippage_pips: float = 0.1
    #: 損切り（逆指値）の不利なずれ（pips）。値が走っているときに執行されるので大きめ
    stop_slippage_pips: float = 0.3

    def fill(self, mid: float, buying: bool, inst: FxInstrument, extra_pips: float | None = None) -> float:
        """仲値から、実際に約定する価格。自分に不利な側へずらす。"""
        slip = self.slippage_pips if extra_pips is None else extra_pips
        offset = (self.spread_pips / 2 + slip) * inst.pip
        return mid + offset if buying else mid - offset

    def exit_mark(self, mid: float, units: float, inst: FxInstrument) -> float:
        """いま手仕舞うとしたら付く価格（ずれは含めない）。評価額に使う。"""
        half = self.spread_pips / 2 * inst.pip
        return mid - half if units > 0 else mid + half


@dataclass(frozen=True)
class FxPosition:
    #: 正は買い、負は売り
    units: float = 0.0
    #: 平均の建値
    price: float = 0.0
    stop: float | None = None
    take: float | None = None
    opened_at: Any = None

    @property
    def is_open(self) -> bool:
        return self.units != 0

    @property
    def is_long(self) -> bool:
        return self.units > 0


@dataclass(frozen=True)
class FxTrade:
    """閉じた取引1件。"""

    entry_at: Any
    exit_at: Any
    units: float
    entry_price: float
    exit_price: float
    pnl: float
    #: スプレッドと不利なずれで失った額（参考）
    cost: float
    exit_reason: str


def close_position(position: FxPosition, price: float, at: Any, reason: str, cost: float = 0.0) -> FxTrade:
    pnl = (price - position.price) * position.units
    return FxTrade(position.opened_at, at, position.units, position.price, price, pnl, cost, reason)


def open_position(units: float, price: float, at: Any, stop: float | None, take: float | None) -> FxPosition:
    return FxPosition(units=units, price=price, stop=stop, take=take, opened_at=at)


def unrealized(position: FxPosition, mid: float, costs: FxCosts, inst: FxInstrument) -> float:
    if not position.is_open:
        return 0.0
    return (costs.exit_mark(mid, position.units, inst) - position.price) * position.units


def maintenance_ratio(equity: float, position: FxPosition, mid: float, inst: FxInstrument) -> float:
    """証拠金維持率。建玉がなければ無限大。"""
    if not position.is_open:
        return float("inf")
    margin = inst.required_margin(position.units, mid)
    return equity / margin if margin > 0 else float("inf")


def stop_or_take(
    position: FxPosition, bar_open: float, high: float, low: float, costs: FxCosts, inst: FxInstrument
) -> tuple[float, str] | None:
    """この足で損切り・利確に届いたら、約定価格と理由を返す。

    損切りと利確の両方に届いた足は、損切りが先と見なす（保守的）。
    寄りで既に逆指値を越えていた（窓開け）ときは、寄りの価格で約定する。
    逆指値・指値は仲値ではなく、手仕舞いの側の価格（買いの建玉なら売値）で判定する。
    """
    if not position.is_open:
        return None
    half = costs.spread_pips / 2 * inst.pip
    if position.is_long:
        bid_open, bid_low, bid_high = bar_open - half, low - half, high - half
        if position.stop is not None and bid_low <= position.stop:
            base = min(position.stop, bid_open)
            return base - costs.stop_slippage_pips * inst.pip, "損切り"
        if position.take is not None and bid_high >= position.take:
            return max(position.take, bid_open), "利確"
    else:
        ask_open, ask_low, ask_high = bar_open + half, low + half, high + half
        if position.stop is not None and ask_high >= position.stop:
            base = max(position.stop, ask_open)
            return base + costs.stop_slippage_pips * inst.pip, "損切り"
        if position.take is not None and ask_low <= position.take:
            return min(position.take, ask_open), "利確"
    return None


def losscut_price(
    cash: float, position: FxPosition, costs: FxCosts, inst: FxInstrument
) -> float | None:
    """証拠金維持率がロスカットの基準ちょうどになる仲値。

    有効証拠金 = 現金 + (手仕舞い価格 − 建値) × 数量、必要証拠金 = |数量| × 仲値 ÷ レバレッジ。
    これを「有効証拠金 = 基準 × 必要証拠金」について仲値で解く。
    """
    if not position.is_open:
        return None
    u = position.units
    half = costs.spread_pips / 2 * inst.pip
    r = inst.losscut_ratio / inst.leverage
    sign = 1.0 if u > 0 else -1.0
    # cash + (m - sign*half - p) * u = r * |u| * m
    denominator = u - r * abs(u)
    if denominator == 0:
        return None
    return (position.price * u + sign * half * u - cash) / denominator


def with_stops(position: FxPosition, stop: float | None, take: float | None) -> FxPosition:
    return replace(position, stop=stop, take=take)


@dataclass(frozen=True)
class FxSignal:
    """戦略の判断。足の終値で判断し、次の足の始値で執行する。

    ``target`` は望む建玉の向き（+1 買い / 0 手仕舞い / -1 売り）。数量はエンジンが決める。
    損切り・利確は、約定価格からの pips で指定する。
    """

    target: int
    stop_pips: float | None = None
    take_pips: float | None = None
    reason: str = ""
