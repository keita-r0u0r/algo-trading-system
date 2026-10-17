"""リスク制限（純粋関数）。

売買候補を出す前に、必ず通す関門。**制限は注文を減らす・止める方向にだけ働く。**
売りは常に通す（持ち続けるほうが危険な場面で、出口を塞がないため）。

制限の変更には非対称なルールがある（``is_loosening``）。厳しくする変更は即時、
緩める変更は確認と待機を要する。画面を操作した直後の気分で緩めるのを防ぐため。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .contracts import Order


@dataclass(frozen=True)
class RiskLimits:
    #: 1注文の上限額（円）
    max_order_value: float = 5_000.0
    #: 1銘柄が評価額に占める上限
    max_position_share: float = 0.35
    #: 同時に持てる銘柄数
    max_positions: int = 5
    #: 日次損失の上限（評価額比）。超えた日は、買いを出さない
    max_daily_loss: float = 0.05


@dataclass(frozen=True)
class RiskResult:
    approved: tuple[Order, ...]
    #: 通さなかった注文と理由
    rejected: tuple[tuple[Order, str], ...]
    #: 数量を減らして通した注文と理由
    clipped: tuple[tuple[Order, str], ...]


def is_loosening(old: RiskLimits, new: RiskLimits) -> bool:
    """1つでも緩めている項目があれば True。どの項目も、大きいほど緩い。"""
    before, after = asdict(old), asdict(new)
    return any(after[key] > before[key] for key in before)


def check_orders(
    orders: list[Order],
    *,
    prices: dict[str, float],
    holdings: dict[str, float],
    equity: float,
    day_return: float,
    limits: RiskLimits,
    lot_size: int = 1,
) -> RiskResult:
    """注文を制限に照らして、通す・減らす・止める。

    ``prices`` は注文の見積もりに使う価格（調整前）。``holdings`` は銘柄ごとの保有数量。
    """
    approved: list[Order] = []
    rejected: list[tuple[Order, str]] = []
    clipped: list[tuple[Order, str]] = []

    held_value = {s: q * prices.get(s, 0.0) for s, q in holdings.items() if q > 0}
    open_symbols = set(held_value)
    halted = day_return <= -limits.max_daily_loss

    for order in orders:
        if not order.side.is_buy:
            approved.append(order)
            continue

        price = prices.get(order.symbol)
        if not price or price <= 0:
            rejected.append((order, "価格が分からない"))
            continue
        if halted:
            rejected.append((order, f"日次損失の上限（{limits.max_daily_loss:.0%}）に到達"))
            continue
        if order.symbol not in open_symbols and len(open_symbols) >= limits.max_positions:
            rejected.append((order, f"同時保有の上限（{limits.max_positions}銘柄）"))
            continue

        quantity = float(order.quantity)
        reason = ""
        cap_value = limits.max_order_value
        if cap_value > 0 and quantity * price > cap_value:
            quantity = float(int(cap_value / price / lot_size) * lot_size)
            reason = f"1注文の上限額（{limits.max_order_value:,.0f}円）"
        if equity > 0:
            room = limits.max_position_share * equity - held_value.get(order.symbol, 0.0)
            if quantity * price > room:
                quantity = float(max(int(room / price / lot_size), 0) * lot_size)
                reason = f"1銘柄の上限（評価額の{limits.max_position_share:.0%}）"

        if quantity <= 0:
            rejected.append((order, reason or "数量が0"))
            continue
        if quantity < order.quantity:
            clipped.append((order, reason))
            order = Order(order.symbol, order.side, quantity, reason=order.reason)
        approved.append(order)
        open_symbols.add(order.symbol)
        held_value[order.symbol] = held_value.get(order.symbol, 0.0) + quantity * price

    return RiskResult(tuple(approved), tuple(rejected), tuple(clipped))
