"""比較対象（ベースライン）。

**新しい戦略を作る前に、まずこの2つを回すこと。**

戦略の成績は絶対値では意味を持たない。「買って持つだけ」と
「ランダムに売買する」の2つと比べて、初めて意味が出る。

  - 買って持つだけに負ける  → その戦略を動かす理由がない
  - ランダムの分布の中にいる → 優位性ではなく偶然を見ている

2つめが特に重要で、ほとんどの人がやらない。同じ売買頻度・同じ保有期間で
ランダムに売買したときの成績分布を出せば、自分の戦略の Sharpe や
リターンがその分布のどのあたりにいるかが分かる。**分布の内側なら、
バックテストがどれだけ良く見えても、それは優位性ではない。**
"""

from __future__ import annotations

import numpy as np

from ..application.ports import MarketView
from ..core.contracts import Order, Portfolio, Side
from .base import affordable_symbols, size_by_cash


class BuyAndHold:
    """初日に等金額で買って、最後まで持つ。

    手数料もスリッページも1往復分しかかからないので、
    回転の速い戦略はここに勝つだけでも簡単ではない。
    """

    name = "buy_and_hold"

    def __init__(self, symbols: list[str] | None = None, max_positions: int = 5) -> None:
        self.symbols = symbols
        self.max_positions = max_positions
        self._done = False

    def on_bar(self, view: MarketView, portfolio: Portfolio) -> list[Order]:
        if self._done:
            return []

        universe = self.symbols or view.symbols()
        # 「買える銘柄」に絞ってから枠を切る。先に枠で切ると、
        # 高い銘柄ばかり選ばれて1株も買えないまま終わる
        n = max(self.max_positions, 1)
        universe = affordable_symbols(view, universe, portfolio.cash / n)
        universe = universe[: self.max_positions]
        if not universe:
            return []

        self._done = True
        budget = portfolio.cash / len(universe)
        orders = []
        for symbol in universe:
            price = view.price(symbol)
            quantity = size_by_cash(budget, price, lot=view.lot_size)
            if quantity > 0:
                orders.append(
                    Order(symbol, Side.BUY, quantity, reason="buy_and_hold: 初日に購入")
                )
        return orders


class RandomEntry:
    """ランダムに売買する。優位性の帰無仮説。

    保有期間と売買頻度を検証したい戦略に合わせて設定し、
    seed を変えて何度も回すと成績の分布が得られる。
    自分の戦略がその分布の内側にいるなら、優位性は示せていない。
    """

    name = "random_entry"

    def __init__(
        self,
        holding_days: int = 20,
        entry_probability: float = 0.05,
        max_positions: int = 3,
        seed: int = 0,
    ) -> None:
        self.holding_days = holding_days
        self.entry_probability = entry_probability
        self.max_positions = max_positions
        self._rng = np.random.default_rng(seed)
        self._held_days: dict[str, int] = {}

    def on_bar(self, view: MarketView, portfolio: Portfolio) -> list[Order]:
        orders: list[Order] = []

        # --- 手仕舞い ---
        for symbol, position in list(((p.symbol, p) for p in portfolio.positions)):
            if not position.is_open:
                self._held_days.pop(symbol, None)
                continue
            self._held_days[symbol] = self._held_days.get(symbol, 0) + 1
            if self._held_days[symbol] >= self.holding_days:
                orders.append(
                    Order(symbol, Side.SELL, position.quantity, reason="random: 保有期限")
                )
                self._held_days.pop(symbol, None)

        # --- 新規 ---
        open_count = len(portfolio.open_positions)
        slots = self.max_positions - open_count - len([o for o in orders if o.side is Side.BUY])
        if slots <= 0 or self._rng.random() > self.entry_probability:
            return orders

        budget_each = portfolio.cash / max(slots, 1)
        candidates = affordable_symbols(
            view,
            [s for s in view.symbols() if portfolio.quantity(s) == 0],
            budget_each,
        )
        if not candidates:
            return orders

        picked = self._rng.choice(
            candidates, size=min(slots, len(candidates)), replace=False
        )
        budget = portfolio.cash / max(len(picked), 1)
        for symbol in picked:
            price = view.price(str(symbol))
            quantity = size_by_cash(budget, price, lot=view.lot_size)
            if quantity > 0:
                orders.append(
                    Order(str(symbol), Side.BUY, quantity, reason="random: ランダム新規")
                )
        return orders
