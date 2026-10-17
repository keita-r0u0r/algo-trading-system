"""会計（純粋関数）。

約定をポートフォリオに反映し、往復が閉じたら ``RoundTrip`` を作る。

ここが1円でもずれると、その先の指標はすべて無意味になる。
だから副作用を一切持たせず、小さな入力で手計算と突き合わせられるようにしてある。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from .contracts import (
    DailySnapshot,
    Fill,
    Portfolio,
    Position,
    RoundTrip,
)


def apply_fill(
    portfolio: Portfolio, fill: Fill
) -> tuple[Portfolio, RoundTrip | None]:
    """約定を反映した新しいポートフォリオと、閉じた往復（あれば）を返す。

    部分決済にも対応する。100株持っていて50株売ったら、
    50株ぶんの往復が1件確定し、残り50株は取得単価を保ったまま残る。
    """
    cash = portfolio.cash + fill.cash_delta
    existing = portfolio.position(fill.symbol) or Position(symbol=fill.symbol)

    if fill.side.is_buy:
        total_quantity = existing.quantity + fill.quantity
        # 平均取得単価は手数料を含めない。含めると「いくらで買ったか」が
        # 分からなくなり、往復損益とコストの切り分けができなくなる
        total_cost = existing.cost_basis + fill.price * fill.quantity
        updated = Position(
            symbol=fill.symbol,
            quantity=total_quantity,
            average_price=total_cost / total_quantity if total_quantity else 0.0,
            opened_on=existing.opened_on or fill.date,
        )
        return portfolio.with_cash(cash).with_position(updated), None

    # --- 売り ---
    quantity = min(fill.quantity, existing.quantity)
    basis = existing.average_price * quantity
    proceeds = fill.price * quantity - fill.commission
    trip = RoundTrip(
        symbol=fill.symbol,
        entry_date=existing.opened_on or fill.date,
        exit_date=fill.date,
        quantity=quantity,
        entry_price=existing.average_price,
        exit_price=fill.price,
        pnl=proceeds - basis,
        costs=fill.commission + fill.slippage,
    )
    remaining = existing.quantity - quantity
    updated = Position(
        symbol=fill.symbol,
        quantity=remaining,
        average_price=existing.average_price if remaining > 0 else 0.0,
        opened_on=existing.opened_on if remaining > 0 else None,
    )
    return portfolio.with_cash(cash).with_position(updated), trip


def snapshot(
    portfolio: Portfolio, day: dt.date, prices: dict[str, float]
) -> DailySnapshot:
    """その日の引け時点の記録。"""
    invested = portfolio.market_value(prices)
    return DailySnapshot(
        date=day,
        equity=portfolio.cash + invested,
        cash=portfolio.cash,
        invested=invested,
        position_count=len(portfolio.open_positions),
    )
