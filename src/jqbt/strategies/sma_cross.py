"""移動平均クロス。最初に検証する「たたき台」。

**この戦略に期待してはいけない。** 移動平均クロスは最も広く知られた
ルールで、もし単体で優位性が残っているなら、とっくに消えている。

それでもこれを最初に置く理由は2つある。

  1. エンジンの検算になる。売買のタイミングが目視で追えるほど単純なので、
     取引履歴を手で確認して、約定価格・損益・手数料の計算を検証できる
  2. 「よく知られたルールは効かない」を自分のデータで確認できる。
     ここで大きな利益が出たら、**まずバグと先読みを疑うべき**

パラメータ（短期・長期の日数）を動かして一番良い組み合わせを探すのは、
過学習そのものである。やるなら最適化した期間と検証期間を必ず分けること。
"""

from __future__ import annotations

from ..application.ports import MarketView
from ..core.contracts import Order, Portfolio, Side
from .base import affordable_symbols, size_by_cash


class SmaCross:
    """短期移動平均が長期を上抜けたら買い、下抜けたら売り。

    判定は当日の終値まで。執行は翌営業日の寄り。
    """

    name = "sma_cross"

    def __init__(
        self,
        fast: int = 25,
        slow: int = 75,
        max_positions: int = 3,
        symbols: list[str] | None = None,
    ) -> None:
        if fast >= slow:
            raise ValueError("fast は slow より短くしてください")
        self.fast = fast
        self.slow = slow
        self.max_positions = max_positions
        self.symbols = symbols

    def on_bar(self, view: MarketView, portfolio: Portfolio) -> list[Order]:
        universe = self.symbols or view.symbols()
        orders: list[Order] = []
        buy_candidates: list[str] = []

        for symbol in universe:
            # numpy 配列で受け取る。毎日・全銘柄を舐めるので、
            # pandas の Series を組み立てる費用が支配的になる
            closes = view.values(symbol, "close", lookback=self.slow + 2)
            # 長期線が引けるまでは何もしない。
            # ここを緩めると、上場直後の銘柄で不安定なシグナルが出る
            if len(closes) < self.slow + 1:
                continue

            fast_now = closes[-self.fast :].mean()
            slow_now = closes[-self.slow :].mean()
            previous = closes[:-1]
            fast_prev = previous[-self.fast :].mean()
            slow_prev = previous[-self.slow :].mean()

            crossed_up = fast_prev <= slow_prev and fast_now > slow_now
            crossed_down = fast_prev >= slow_prev and fast_now < slow_now
            holding = portfolio.quantity(symbol) > 0

            if crossed_down and holding:
                orders.append(
                    Order(
                        symbol,
                        "sell",
                        portfolio.quantity(symbol),
                        reason=f"SMA{self.fast}がSMA{self.slow}を下抜け",
                    )
                )
            elif crossed_up and not holding:
                buy_candidates.append(symbol)

        # --- 新規は枠の範囲で ---
        open_count = len(portfolio.open_positions)
        selling = len([o for o in orders if o.side is Side.SELL])
        slots = self.max_positions - open_count + selling
        if slots <= 0 or not buy_candidates:
            return orders

        # 資金は「いま空いている現金」だけで配分する。
        # 売却予定分を当てにすると、実際には間に合わず資金不足で弾かれる
        budget = portfolio.cash / min(len(buy_candidates), slots)
        buy_candidates = affordable_symbols(view, buy_candidates, budget)
        for symbol in buy_candidates[:slots]:
            price = view.price(symbol)
            if not price:
                continue
            quantity = size_by_cash(budget, price, lot=view.lot_size)
            if quantity > 0:
                orders.append(
                    Order(
                        symbol,
                        "buy",
                        quantity,
                        reason=f"SMA{self.fast}がSMA{self.slow}を上抜け",
                    )
                )
        return orders
