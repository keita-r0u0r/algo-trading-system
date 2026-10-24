"""FX の日中売買のバックテスト（足を1本ずつ進める）。

## 1本の足の中での順番

  1. 前の足の終値で出た判断を、この足の **始値** で執行する
  2. 損切り・利確に届いたか（両方なら損切りが先、と見なす）
  3. ロスカットの価格に届いたか。届いたら **検証はそこで終わり**
  4. その日の最後の足なら、建玉を手仕舞う（日をまたがない）。データの最後の足でも手仕舞う
  5. 終値で評価額を記録する
  6. 戦略が終値で判断する（次の足の始値で執行）

戦略に渡すのは、その足までの配列（``FxView``）だけ。未来の足には触れられない。

日をまたいで持つときは、営業日が替わるたびにスワップ（金利差）を受け取る・払う。
暦の日数で数える（金曜から月曜は3日分）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import numpy as np
import pandas as pd

from ..core.fx.metrics import summarize
from ..core.fx.model import (
    FxCosts,
    FxInstrument,
    FxPosition,
    FxSignal,
    RiskProfile,
    FxTrade,
    close_position,
    losscut_price,
    open_position,
    size_units,
    stop_or_take,
    unrealized,
)


@dataclass(frozen=True)
class FxView:
    """その足までの世界。配列は終端が「いま」の足（未来は含まない）。"""

    index: int
    time: Any
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    #: その日の残りの足の数（この足を含まない）
    bars_left_today: int


class FxStrategy(Protocol):
    name: str

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None: ...


@dataclass(frozen=True)
class FxConfig:
    initial_cash: float = 10_000.0
    instrument: FxInstrument = field(default_factory=FxInstrument)
    costs: FxCosts = field(default_factory=FxCosts)
    #: 1回の取引の数量（通貨）。None なら取引単位の最小
    units: float | None = None
    #: 必要証拠金が有効証拠金のこの割合を超える建玉は作らない
    max_margin_use: float = 0.8
    #: その日の残りがこの本数以下なら、新規の建玉を作らない
    no_entry_last_bars: int = 15
    #: 日をまたがない（デイトレ）
    flat_at_day_end: bool = True
    #: スプレッドがこれ（pips）を超える足では、新規の建玉を作らない（朝の切り替えなど）。None なら制限なし
    max_entry_spread_pips: float | None = None
    #: スワップ（円/通貨/日）。受け取りは正、支払いは負。日をまたいで持つときだけ効く
    swap_long: float = 0.0
    swap_short: float = 0.0
    #: リスクの度合い。あれば数量はここから決め（``units`` は使わない）、1日の損失の上限も効かせる
    risk: RiskProfile | None = None
    #: この時刻より前の足は「助走」。戦略には見せて状態（過去20日の高値など）を作らせるが、売買しない。
    #: 月ごとに資金を戻して評価するとき、月の初めに過去の足を知らない状態で始まらないようにする
    trade_from: Any = None


@dataclass
class FxResult:
    config: FxConfig
    strategy: str
    equity: np.ndarray
    days: list
    trades: list[FxTrade]
    losscut: bool = False
    losscut_at: Any = None
    skipped_entries: int = 0

    @property
    def summary(self) -> dict[str, Any]:
        out = summarize(self.config.initial_cash, self.equity, self.days, self.trades, self.losscut)
        out["losscut_at"] = self.losscut_at
        out["skipped_entries"] = self.skipped_entries
        return out


def run_fx(bars: pd.DataFrame, strategy: FxStrategy, config: FxConfig = FxConfig()) -> FxResult:
    """``bars`` は時刻を索引に、open / high / low / close（仲値）の列を持つ。

    次の列は、あれば使う（実際の相場のデータ）。

      * ``spread``  その足のスプレッド（pips）。無ければ ``config.costs.spread_pips`` で一定
      * ``session`` その足が属する営業日。無ければ時刻の日付。日をまたがない「1日」の区切りになる
      * ``swap_long`` / ``swap_short`` その日のスワップ（円/通貨/日）。無ければ ``config`` の一定の値
    """
    inst, base_costs = config.instrument, config.costs
    o = bars["open"].to_numpy(dtype=float)
    h = bars["high"].to_numpy(dtype=float)
    lo = bars["low"].to_numpy(dtype=float)
    c = bars["close"].to_numpy(dtype=float)
    spread = bars["spread"].to_numpy(dtype=float) if "spread" in bars else None
    swaps = (bars["swap_long"].to_numpy(dtype=float), bars["swap_short"].to_numpy(dtype=float)) \
        if "swap_long" in bars and "swap_short" in bars else None
    times = bars.index
    day_of = np.asarray(bars["session"]) if "session" in bars else np.array([t.date() for t in times])
    n = len(c)
    # その日の残りの本数（この足を含まない）
    left = np.zeros(n, dtype=int)
    for i in range(n - 2, -1, -1):
        left[i] = left[i + 1] + 1 if day_of[i + 1] == day_of[i] else 0

    cash = config.initial_cash
    position = FxPosition()
    pending: FxSignal | None = None
    trades: list[FxTrade] = []
    equity = np.full(n, np.nan)
    skipped = 0
    base_units = config.units or inst.min_units
    risk = config.risk
    day_start_equity = config.initial_cash
    halted_day = None
    result = FxResult(config, getattr(strategy, "name", "fx"), equity, list(day_of), trades)

    def exit_at(price: float, at: Any, reason: str, mid: float) -> None:
        nonlocal cash, position
        trade = close_position(position, price, at, reason, cost=abs(price - mid) * abs(position.units))
        cash += trade.pnl
        trades.append(trade)
        position = FxPosition()

    costs = base_costs
    for i in range(n):
        t = times[i]
        if spread is not None:
            costs = replace(base_costs, spread_pips=spread[i])
        # 助走: 戦略に見せるだけ。売買も評価もしない
        if config.trade_from is not None and t < config.trade_from:
            equity[i] = cash
            if i < n - 1:
                strategy.decide(FxView(i, t, o[: i + 1], h[: i + 1], lo[: i + 1], c[: i + 1], int(left[i])), position)
            continue

        # 0. 営業日が替わったら、持ち越した建玉のスワップ。1日の損失の基準もここで取り直す
        if i > 0 and day_of[i] != day_of[i - 1]:
            if position.is_open:
                days = max((pd.Timestamp(day_of[i]) - pd.Timestamp(day_of[i - 1])).days, 1)
                if swaps is not None:
                    rate = swaps[0][i] if position.is_long else swaps[1][i]
                else:
                    rate = config.swap_long if position.is_long else config.swap_short
                cash += abs(position.units) * rate * days
            day_start_equity = equity[i - 1]

        # 1. 前の足の判断を、この足の始値で執行
        if pending is not None:
            direction = 0 if not position.is_open else (1 if position.is_long else -1)
            if pending.target != direction:
                if position.is_open:
                    exit_at(costs.fill(o[i], not position.is_long, inst), t, pending.reason or "シグナル", o[i])
                too_wide = config.max_entry_spread_pips is not None and costs.spread_pips > config.max_entry_spread_pips
                size = base_units
                if risk is not None:
                    # 証拠金の上限に収まるところまで減らす（減らした結果が最小の注文数量に届かなければ入らない）
                    affordable = int(max(cash, 0) * config.max_margin_use / inst.entry_margin(1, o[i]))
                    size = min(size_units(risk, cash, o[i], pending.stop_pips, inst), float(affordable))
                    size = size if size >= inst.min_units else 0.0
                if pending.target != 0 and (too_wide or size == 0 or halted_day == day_of[i]):
                    skipped += 1
                elif pending.target != 0:
                    units = size * pending.target
                    margin = inst.entry_margin(units, o[i])
                    if margin > max(cash, 0) * config.max_margin_use:
                        skipped += 1
                    else:
                        price = costs.fill(o[i], pending.target > 0, inst)
                        sign = 1 if pending.target > 0 else -1
                        stop = price - sign * pending.stop_pips * inst.pip if pending.stop_pips else None
                        take = price + sign * pending.take_pips * inst.pip if pending.take_pips else None
                        position = open_position(units, price, t, stop, take)
            pending = None

        # 2. 損切り・利確
        hit = stop_or_take(position, o[i], h[i], lo[i], costs, inst)
        if hit is not None:
            price, reason = hit
            exit_at(price, t, reason, c[i])

        # 3. ロスカット（届いたら検証を終える）
        if position.is_open:
            level = losscut_price(cash, position, costs, inst)
            worst = lo[i] if position.is_long else h[i]
            crossed = level is not None and (worst <= level if position.is_long else worst >= level)
            if crossed:
                mid = min(level, o[i]) if position.is_long else max(level, o[i])
                price = costs.fill(mid, not position.is_long, inst, extra_pips=costs.stop_slippage_pips)
                price += -costs.losscut_fee if position.is_long else costs.losscut_fee
                exit_at(price, t, "強制ロスカット", mid)
                equity[i] = cash
                result.losscut, result.losscut_at = True, t
                equity[i + 1 :] = cash
                break

        # 4. 日の終わりに手仕舞う
        if config.flat_at_day_end and left[i] == 0 and position.is_open:
            exit_at(costs.fill(c[i], not position.is_long, inst), t, "日の終わり", c[i])
        elif i == n - 1 and position.is_open:
            exit_at(costs.fill(c[i], not position.is_long, inst), t, "期間の終わり", c[i])

        # 5. 評価額
        equity[i] = cash + unrealized(position, c[i], costs, inst)

        # 5'. 1日の損失の上限（リスクの度合いがあるとき）。届いたら手仕舞い、その日は新規をやめる
        if risk is not None and halted_day != day_of[i] and equity[i] <= day_start_equity * (1 - risk.daily_loss_limit):
            halted_day = day_of[i]
            if position.is_open:
                exit_at(costs.fill(c[i], not position.is_long, inst), t, "1日の損失の上限", c[i])
                equity[i] = cash

        # 6. 判断（最後の足の判断は執行先がないので捨てる）
        if i == n - 1:
            break
        view = FxView(i, t, o[: i + 1], h[: i + 1], lo[: i + 1], c[: i + 1], int(left[i]))
        signal = strategy.decide(view, position)
        if signal is not None:
            opening = signal.target != 0 and not position.is_open
            if opening and left[i] <= config.no_entry_last_bars:
                signal = None
        pending = signal

    result.skipped_entries = skipped
    return result
