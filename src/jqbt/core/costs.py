"""売買コストのモデル。

「バックテストでは1000円で買えた → 実際には1002円でしか買えない」
の差をここに集約する。コストを甘く見積もると、優位性がないものを
あるように見せてしまう。この1ファイルの誤りが検証全体を無意味にする。

コストは3つに分けて考える。混ぜると、どれが効いているか分からなくなる。

  1. スプレッド  : 呼値の刻みで決まる。最良気配で買って最良気配で売る限り必ず払う
  2. マーケットインパクト : 自分の注文が板を食う分。注文サイズ / 出来高 で効く
  3. 手数料      : 証券会社に払う分。2026年時点の国内株は無料のところが多い

税金（申告分離課税 20.315%）は取引ごとではなく年間損益に掛かるため、
ここではなく metrics 側で扱う。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 東証の呼値（内国株式、TOPIX構成銘柄以外の通常の刻み）
#: 価格帯の上限 → 刻み幅
TICK_TABLE: tuple[tuple[float, float], ...] = (
    (3_000, 1),
    (5_000, 5),
    (30_000, 10),
    (50_000, 50),
    (300_000, 100),
    (500_000, 500),
    (3_000_000, 1_000),
    (5_000_000, 5_000),
    (30_000_000, 10_000),
    (float("inf"), 100_000),
)

#: 単元株数。国内株は2018年10月以降すべて100株に統一されている
LOT_SIZE = 100


def tick_size(price: float) -> float:
    """その価格帯の呼値の刻み。"""
    for upper, tick in TICK_TABLE:
        if price <= upper:
            return tick
    return TICK_TABLE[-1][1]


def round_to_tick(price: float, up: bool) -> float:
    """呼値に丸める。買いは切り上げ、売りは切り下げ（自分に不利な側）。

    有利な側に丸めると、バックテストが現実より良く出る。
    """
    tick = tick_size(price)
    units = price / tick
    rounded = (int(units) + 1 if units % 1 else int(units)) if up else int(units)
    return rounded * tick


@dataclass(frozen=True)
class CostModel:
    """約定価格とコストの見積もり。

    既定値は「かなり楽観的」でも「かなり悲観的」でもない中間を狙っているが、
    **実運用の実測値に置き換えるまでは仮定でしかない。**
    ペーパートレードで実測し、ここを更新すること。
    """

    #: 最良気配で約定できると仮定するか。False なら半スプレッドを追加で払う
    cross_spread: bool = True
    #: マーケットインパクト係数。注文サイズ / 当日出来高 に掛ける
    #: 0.1 なら「出来高の10%を食うと1%滑る」という意味合い
    impact_coefficient: float = 0.1
    #: 1注文あたりの手数料（円）。2026年の国内株は無料の証券会社が多い
    commission_per_order: float = 0.0
    #: 約定代金に対する手数料率
    commission_rate: float = 0.0
    #: 出来高のこの割合までしか約定しないと仮定する
    max_participation: float = 0.1
    #: 参照価格に対する追加スプレッド（片道・比率）。単元未満株（S株・ミニ株）用。
    #: 単元未満株は証券会社が提示する価格で約定し、呼値の半分より広く取られる。
    #: **証券会社ごとの実測値に置き換えるまでは仮定でしかない。**
    extra_spread_rate: float = 0.0

    def fill_price(
        self, reference_price: float, side: str, quantity: float, bar_volume: float
    ) -> float:
        """約定価格。参照価格（通常は翌日始値）から、不利な方向へずらす。

        ``side`` は "buy" / "sell"。
        """
        price = reference_price
        buying = getattr(side, "value", side) == "buy"

        if self.cross_spread:
            # 最良気配を取りに行く以上、半スプレッドは必ず払う
            half = tick_size(price) / 2
            price += half if buying else -half

        if self.extra_spread_rate:
            # 自分に不利な側へずらす。買いは高く、売りは安く
            price *= (1 + self.extra_spread_rate) if buying else (1 - self.extra_spread_rate)

        if self.impact_coefficient and bar_volume > 0:
            participation = (quantity / bar_volume) if bar_volume else 0.0
            impact = price * self.impact_coefficient * participation
            price += impact if buying else -impact

        return max(round_to_tick(price, up=buying), tick_size(reference_price))

    def commission(self, notional: float) -> float:
        if notional <= 0:
            return 0.0
        return self.commission_per_order + notional * self.commission_rate

    def fillable_quantity(self, requested: float, bar_volume: float) -> float:
        """その日の出来高から、実際に約定できると見なす数量。

        出来高の ``max_participation`` を超える注文は部分約定とする。
        ここを無制限にすると、流動性のない銘柄で理論上の利益が出てしまう。

        単元株への丸めはここではやらない。単元の強制はエンジン側の
        ``enforce_lot_size`` が持つ責務で、2箇所に置くと片方を無効にしても
        もう片方が効いてしまう。
        """
        if bar_volume <= 0:
            return 0.0
        return float(min(requested, bar_volume * self.max_participation))


#: 手数料無料・楽観側。実運用前の感度分析の片側として使う
OPTIMISTIC = CostModel(cross_spread=False, impact_coefficient=0.0)
#: 既定。半スプレッド + 小さめのインパクト
REALISTIC = CostModel()
#: 悲観側。スプレッドを1ティック丸ごと + インパクト大
PESSIMISTIC = CostModel(impact_coefficient=0.3, commission_rate=0.0005)
