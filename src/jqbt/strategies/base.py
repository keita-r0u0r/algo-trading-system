"""戦略のインターフェイス。

戦略が受け取るのは ``MarketView``（当日までの世界）と ``Portfolio`` だけ。
返すのは翌営業日の寄りで執行する注文のリスト。

**戦略は現在時刻を知らないし、未来のデータにも触れられない。**
これは規約ではなく、MarketView が物理的にそうしている。
"""

from __future__ import annotations

from typing import Protocol

from ..application.ports import MarketView
from ..core.contracts import Order, Portfolio
from ..core.costs import LOT_SIZE


class Strategy(Protocol):
    """``application.ports.Strategy`` の再輸出。戦略側はこちらを見ればよい。"""

    name: str

    def on_bar(self, view: MarketView, portfolio: Portfolio) -> list[Order]:
        """当日の引け時点での判断。翌営業日の寄りで執行される注文を返す。"""
        ...


def size_by_cash(
    cash: float, price: float, fraction: float = 1.0, lot: int = LOT_SIZE
) -> float:
    """使える資金から単元株単位の数量を出す。

    **単元株の制約は少額運用では決定的に効く。**
    資金30万円で株価3,000円の銘柄は1単元（30万円）で全額を使い切るため、
    分散がそもそも成立しない。この関数が0を返すのは失敗ではなく、
    「その資金ではその銘柄を買えない」という事実である。
    """
    if price <= 0 or cash <= 0:
        return 0.0
    budget = cash * fraction
    units = int(budget / (price * lot))
    return float(units * lot)


def affordable_symbols(
    view, symbols: list[str], budget_per_position: float, lot: int | None = None
) -> list[str]:
    """その予算で1単元以上買える銘柄だけに絞る。

    少額運用では、ここで母集団が激減する。買えない銘柄を含んだまま
    「上位N銘柄」を選ぶと、実際には1銘柄も買えないまま検証が終わる。

    **判定は調整前の終値で行う。** 調整済みの終値は、後から起きた株式分割で
    過去の価格が割り引かれる。それで判定すると、当時は買えなかった銘柄を
    「買える」と見なす。分割は上昇のあとに起きやすいので、将来を知っているのと
    同じ偏りが成績に乗る（実測で、ある候補が +139% から +4% に落ちた）。
    """
    lot = lot or view.lot_size
    frame = view.today()
    column = "raw_close" if "raw_close" in frame.columns else "close"
    result = []
    for symbol in symbols:
        if symbol not in frame.index:
            continue
        price = frame.at[symbol, column]
        if price == price and price > 0 and price * lot <= budget_per_position:
            result.append(symbol)
    return result


def equal_weight_targets(
    symbols: list[str], total_value: float, max_positions: int
) -> float:
    """1銘柄あたりに割り当てる金額。"""
    if not symbols or max_positions <= 0:
        return 0.0
    return total_value / min(len(symbols), max_positions)
