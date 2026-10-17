"""約定判定（純粋関数）。

「この注文は、この足で、いくらで、何株約定するか」だけを決める。
日付を進めることも、結果を溜めることもしない。それは application の仕事。

ここを純粋にしておくと、約定ルールを単体でテストできる。
バックテストを1本回さないと約定価格が確かめられない状態だと、
スリッページの仮定を変えたときに何が起きたか分からなくなる。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .contracts import Bar, Fill, Order, Portfolio, RejectReason, Side
from .costs import LOT_SIZE, CostModel


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """約定判定に必要な外部条件。すべて値として渡す。"""

    date: dt.date
    bar: Bar
    costs: CostModel
    enforce_lot_size: bool = True
    #: 売買単位。国内株は100株。単元未満株（S株・ミニ株）なら1
    lot_size: int = LOT_SIZE


def simulate(
    order: Order, context: ExecutionContext, portfolio: Portfolio
) -> Fill | RejectReason:
    """1件の注文を約定させる。約定しない場合は理由を返す。

    参照価格は **約定日の始値**。前日の終値ではない。
    「T日の引けで判断し、T+1日の寄りで約定する」という約束の実装がここ。
    """
    bar = context.bar
    reference = bar.open
    if not (reference > 0):
        return RejectReason.NO_QUOTE

    # ストップ高・安で張り付いた日は、その方向には約定しないものとする
    if bar.limit_up and order.side.is_buy:
        return RejectReason.LIMIT_UP
    if bar.limit_down and not order.side.is_buy:
        return RejectReason.LIMIT_DOWN

    quantity = context.costs.fillable_quantity(order.quantity, bar.volume)
    if context.enforce_lot_size:
        quantity = _floor_to_lot(quantity, context.lot_size)
    if quantity <= 0:
        return RejectReason.NO_VOLUME if bar.volume <= 0 else RejectReason.BELOW_LOT_SIZE

    if not order.side.is_buy:
        quantity = min(quantity, portfolio.quantity(order.symbol))
        if quantity <= 0:
            return RejectReason.NO_POSITION

    price = context.costs.fill_price(reference, order.side.value, quantity, bar.volume)

    if order.side.is_buy:
        quantity = _affordable_quantity(
            quantity,
            price,
            portfolio.cash,
            context.costs,
            context.enforce_lot_size,
            context.lot_size,
        )
        if quantity <= 0:
            return RejectReason.INSUFFICIENT_CASH

    notional = price * quantity
    return Fill(
        date=context.date,
        symbol=order.symbol,
        side=order.side,
        quantity=quantity,
        price=price,
        commission=context.costs.commission(notional),
        slippage=abs(price - reference) * quantity,
        reason=order.reason,
    )


def _floor_to_lot(quantity: float, lot_size: int = LOT_SIZE) -> float:
    return float(int(quantity / lot_size) * lot_size)


def _affordable_quantity(
    quantity: float,
    price: float,
    cash: float,
    costs: CostModel,
    enforce_lot_size: bool,
    lot_size: int = LOT_SIZE,
) -> float:
    """資金の範囲まで数量を落とす。

    全部諦めるのではなく買える分だけ買うのは、実際の運用がそうなるため。
    ここで全部諦めると、資金制約の影響を過大に見積もることになる。
    """
    if price <= 0:
        return 0.0
    if price * quantity + costs.commission(price * quantity) <= cash:
        return quantity

    # 手数料を含めて収まる最大数量を逆算する
    affordable = quantity
    for _ in range(4):  # 手数料が数量に依存するので数回だけ反復する
        fee = costs.commission(price * affordable)
        affordable = max((cash - fee) / price, 0.0)
        if enforce_lot_size:
            affordable = _floor_to_lot(affordable, lot_size)
        if affordable <= 0:
            return 0.0
        if price * affordable + costs.commission(price * affordable) <= cash:
            return affordable
    return 0.0
