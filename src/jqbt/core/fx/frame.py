"""判断の枠（純粋関数）。AI やルールの判断が、ここを通らなければ実行しない。

AI には通貨ペア・向き・リスクの割合・損切り・利確を **自由に** 決めさせる。そのかわり、
次の前提はコードで強制し、AI には変えさせない。

  * 選べる通貨ペアは ``symbols`` だけ
  * 新規の建玉には必ず損切りを付ける。幅は ``min_stop_pips``〜``max_stop_pips``
  * 1回の損切りで失う割合は、リスクの度合いの上限まで（数量はそこから逆算する）
  * 同時に持つ建玉は1つ。持っている間の新規は、いったん閉じてから
  * スプレッドが広すぎる時、1日の損失の上限に届いた日、キルスイッチが入っている時は、新規を作らない
  * 閉じる判断は、いつでも通す（リスクを下げる方向なので）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import FxInstrument, RiskProfile

ACTIONS = ("hold", "open_long", "open_short", "close")


@dataclass(frozen=True)
class TradingFrame:
    symbols: tuple[str, ...]
    risk: RiskProfile
    min_stop_pips: float = 10.0
    max_stop_pips: float = 150.0
    max_entry_spread_pips: float = 1.0
    #: 利確を付けるなら、損切りの幅のこの倍数以上（小さすぎる利確でコスト負けしないように）
    min_take_to_stop: float = 0.5

    def describe(self) -> dict[str, Any]:
        """AI に渡す、枠の説明。"""
        return {
            "symbols": list(self.symbols),
            "max_risk_per_trade": self.risk.risk_per_trade,
            "max_leverage": self.risk.max_leverage,
            "daily_loss_limit": self.risk.daily_loss_limit,
            "stop_pips_range": [self.min_stop_pips, self.max_stop_pips],
            "max_entry_spread_pips": self.max_entry_spread_pips,
            "positions_at_once": 1,
        }


@dataclass(frozen=True)
class Approved:
    """枠を通った判断。数量は確定している（0 は閉じるだけ）。"""

    action: str
    symbol: str
    units: float
    stop_pips: float | None
    take_pips: float | None
    risk_fraction: float


def size_for(frame: TradingFrame, risk_fraction: float, stop_pips: float, equity: float, price: float,
             inst: FxInstrument, margin_cap: float) -> float:
    """リスクの割合と損切りの幅から数量を決める。レバレッジ・証拠金の上限で切る。"""
    if equity <= 0 or price <= 0 or stop_pips <= 0:
        return 0.0
    by_risk = equity * risk_fraction / (stop_pips * inst.pip)
    by_leverage = equity * frame.risk.max_leverage / price
    by_margin = equity * margin_cap / inst.entry_margin(1, price)
    units = float(int(min(by_risk, by_leverage, by_margin)))
    return units if units >= inst.min_units else 0.0


def check(
    decision: dict[str, Any],
    frame: TradingFrame,
    *,
    equity: float,
    holding: str | None,
    spread_pips: dict[str, float],
    prices: dict[str, float],
    instruments: dict[str, FxInstrument],
    halted: bool,
    margin_cap: float = 0.8,
) -> tuple[Approved | None, str]:
    """判断を枠に照らす。返すのは (通った判断, 通らなかった理由)。"""
    action = decision.get("action")
    symbol = str(decision.get("symbol") or "").strip()
    if action not in ACTIONS:
        return None, f"知らない判断（{action}）"
    if action == "hold":
        return None, ""
    if action == "close":
        if holding is None:
            return None, "閉じる建玉が無い"
        return Approved("close", holding, 0.0, None, None, 0.0), ""

    # 新規
    if holding is not None:
        return None, f"建玉（{holding}）を持っている間は新規を作らない。先に閉じる"
    if symbol not in frame.symbols:
        return None, f"枠に無い通貨ペア（{symbol}）"
    if halted:
        return None, "1日の損失の上限に届いた日、またはキルスイッチが入っている"
    spread = spread_pips.get(symbol)
    if spread is None or symbol not in prices:
        return None, f"{symbol} の値段が取れていない"
    if spread > frame.max_entry_spread_pips:
        return None, f"スプレッドが広い（{spread:.1f} pips > {frame.max_entry_spread_pips:g}）"
    stop = _number(decision.get("stop_pips"))
    if stop is None or not frame.min_stop_pips <= stop <= frame.max_stop_pips:
        return None, f"損切りの幅が枠の外（{stop}。{frame.min_stop_pips:g}〜{frame.max_stop_pips:g} pips）"
    take = _number(decision.get("take_pips")) or None
    if take is not None and take < stop * frame.min_take_to_stop:
        return None, f"利確の幅が小さすぎる（{take} < 損切りの {frame.min_take_to_stop:g} 倍）"
    risk_fraction = _number(decision.get("risk_fraction")) or 0.0
    if risk_fraction <= 0:
        return None, "リスクの割合が0以下"
    risk_fraction = min(risk_fraction, frame.risk.risk_per_trade)
    units = size_for(frame, risk_fraction, stop, equity, prices[symbol], instruments[symbol], margin_cap)
    if units <= 0:
        return None, "数量が最小の注文数量に届かない（資金・リスクの割合・損切りの幅の組み合わせ）"
    sign = 1 if action == "open_long" else -1
    return Approved(action, symbol, units * sign, stop, take, risk_fraction), ""


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN を弾く
