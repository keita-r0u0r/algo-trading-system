"""FX の前向きのペーパー運用（仮想の約定）。1時間に1回、外から呼ばれて少しずつ進む。

過去の検証（``fx_backtest``）と違い、**その時点の本物の値段** で判断・約定させて記録する。
AI の判断は過去のデータでは試せない（学習した期間の値動きを知っている）ので、ここで前向きに記録するしかない。

## 1回の実行でやること

  1. 前回から今までの1分足で、建玉の損切り・利確・ロスカット・1日の損失の上限を確かめる（``advance``）
  2. 判断する（AI・ルール・基準）。判断は必ず枠（``core.fx.frame``）を通す
  3. 通った判断を、今の買値・売値で約定させる（``execute``）
  4. 口座の状態を記録する

口座は判断する者ごとに分ける（AI / ルール / 買って持つだけ / 一切売買しない）。同じ時刻・同じ値段で並べて比べる。
注文は出さない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import pandas as pd

from ..core.fx.frame import Approved
from ..core.fx.model import FxCosts, FxInstrument, FxPosition, losscut_price, stop_or_take, unrealized


@dataclass(frozen=True)
class PaperPosition:
    symbol: str
    units: float
    price: float
    stop: float | None
    take: float | None
    opened_at: str
    reason: str = ""


@dataclass(frozen=True)
class PaperAccount:
    name: str
    initial: float
    cash: float
    position: PaperPosition | None = None
    #: 最後に確かめた1分足の時刻（日本時間、ISO 形式）
    last_time: str | None = None
    session: str | None = None
    day_start_equity: float | None = None
    halted_session: str | None = None
    #: 最後に評価した額（建玉の含み損益を含む）
    equity: float | None = None
    stopped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "PaperAccount":
        position = raw.get("position")
        names = {f for f in PaperAccount.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in names}
        data["position"] = PaperPosition(**position) if position else None
        return PaperAccount(**data)


@dataclass
class Step:
    """1回の実行で起きたこと（記録する出来事）。"""

    account: PaperAccount
    events: list[dict[str, Any]] = field(default_factory=list)


def _as_fx(position: PaperPosition) -> FxPosition:
    return FxPosition(units=position.units, price=position.price, stop=position.stop, take=position.take,
                      opened_at=position.opened_at)


def _close(step: Step, price: float, at: str, reason: str) -> None:
    account, position = step.account, step.account.position
    assert position is not None
    pnl = (price - position.price) * position.units
    step.account = replace(account, cash=account.cash + pnl, position=None)
    step.events.append({"kind": "close", "account": account.name, "symbol": position.symbol, "units": position.units,
                        "entry_price": position.price, "exit_price": price, "pnl": pnl, "opened_at": position.opened_at,
                        "at": at, "reason": reason})


def advance(
    account: PaperAccount,
    bars: pd.DataFrame,
    costs: FxCosts,
    inst: FxInstrument,
    *,
    daily_loss_limit: float,
    swap_long: float = 0.0,
    swap_short: float = 0.0,
) -> Step:
    """前回から今までの1分足で、建玉を確かめる。

    ``bars`` は持っている通貨ペア（無ければ基準の通貨ペア）の1分足で、仲値の open/high/low/close・
    spread（pips）・session（営業日）の列を持つ。時刻は日本時間。
    """
    step = Step(account)
    if account.stopped or bars.empty:
        return step
    after = pd.Timestamp(account.last_time) if account.last_time else None
    new = bars[bars.index > after] if after is not None else bars.iloc[-1:]
    for t, bar in new.iterrows():
        acc = step.account
        at = t.isoformat()
        session = str(bar["session"])
        bar_costs = replace(costs, spread_pips=float(bar["spread"]))
        # 営業日が替わったら: スワップ、1日の損失の基準を取り直す
        if acc.session is not None and session != acc.session:
            cash = acc.cash
            if acc.position is not None:
                days = max((pd.Timestamp(session) - pd.Timestamp(acc.session)).days, 1)
                rate = swap_long if acc.position.units > 0 else swap_short
                cash += abs(acc.position.units) * rate * days
            acc = replace(acc, cash=cash, day_start_equity=acc.equity if acc.equity is not None else cash,
                          halted_session=None)
        if acc.session is None:
            acc = replace(acc, day_start_equity=acc.cash)
        step.account = acc = replace(acc, session=session, last_time=at)

        position = acc.position
        if position is not None:
            fx = _as_fx(position)
            hit = stop_or_take(fx, bar["open"], bar["high"], bar["low"], bar_costs, inst)
            if hit is not None:
                _close(step, hit[0], at, hit[1])
            else:
                level = losscut_price(acc.cash, fx, bar_costs, inst)
                worst = bar["low"] if fx.is_long else bar["high"]
                if level is not None and (worst <= level if fx.is_long else worst >= level):
                    mid = min(level, bar["open"]) if fx.is_long else max(level, bar["open"])
                    price = bar_costs.fill(mid, not fx.is_long, inst, extra_pips=costs.stop_slippage_pips)
                    price += -costs.losscut_fee if fx.is_long else costs.losscut_fee
                    _close(step, price, at, "強制ロスカット")
                    step.account = replace(step.account, stopped=True)
                    step.events.append({"kind": "stopped", "account": acc.name, "at": at, "reason": "ロスカット"})
                    break

        acc = step.account
        equity = acc.cash + (unrealized(_as_fx(acc.position), bar["close"], bar_costs, inst) if acc.position else 0.0)
        step.account = acc = replace(acc, equity=equity)
        start = acc.day_start_equity or acc.cash
        if acc.halted_session != session and equity <= start * (1 - daily_loss_limit):
            if acc.position is not None:
                _close(step, bar_costs.fill(bar["close"], acc.position.units < 0, inst), at, "1日の損失の上限")
            step.account = replace(step.account, halted_session=session, equity=step.account.cash)
            step.events.append({"kind": "halted", "account": acc.name, "at": at, "session": session})
    return step


def execute(
    account: PaperAccount,
    approved: Approved,
    *,
    bid: float,
    ask: float,
    at: dt.datetime,
    costs: FxCosts,
    inst: FxInstrument,
    reason: str,
) -> Step:
    """枠を通った判断を、今の買値・売値で約定させる（仮想）。"""
    step = Step(account)
    stamp = at.isoformat(timespec="seconds")
    slip = costs.slippage_pips * inst.pip
    if approved.action == "close":
        position = account.position
        if position is None:
            return step
        base = bid if position.units > 0 else ask
        price = base - slip - base * costs.fee_rate if position.units > 0 else base + slip + base * costs.fee_rate
        _close(step, price, stamp, reason or "閉じる判断")
        return step
    buying = approved.units > 0
    base = ask if buying else bid
    price = base + slip + base * costs.fee_rate if buying else base - slip - base * costs.fee_rate
    sign = 1 if buying else -1
    stop = price - sign * approved.stop_pips * inst.pip if approved.stop_pips else None
    take = price + sign * approved.take_pips * inst.pip if approved.take_pips else None
    position = PaperPosition(approved.symbol, approved.units, price, stop, take, stamp, reason[:200])
    step.account = replace(account, position=position)
    step.events.append({"kind": "open", "account": account.name, "symbol": approved.symbol, "units": approved.units,
                        "price": price, "stop": stop, "take": take, "at": stamp, "reason": reason[:200],
                        "risk_fraction": approved.risk_fraction})
    return step


def mark(account: PaperAccount, bid: float, ask: float) -> float:
    """今の値段で手仕舞ったとしたらの評価額（手数料・ずれは含めない）。"""
    if account.position is None:
        return account.cash
    p = account.position
    exit_price = bid if p.units > 0 else ask
    return account.cash + (exit_price - p.price) * p.units


def hourly_breakout_target(hourly: pd.DataFrame, window: int = 20) -> int:
    """完成した1時間足（high/low/close）の最後の足が、直近 ``window`` 本の高値・安値を抜けたか（+1 / 0 / −1）。

    ``strategies.fx.HourlyBreakout`` と同じ決まり。
    """
    if len(hourly) <= window:
        return 0
    last, previous = hourly.iloc[-1], hourly.iloc[-1 - window : -1]
    if last["close"] > previous["high"].max():
        return 1
    if last["close"] < previous["low"].min():
        return -1
    return 0


def moves_after(bars: pd.DataFrame, at: pd.Timestamp, hours: tuple[int, ...] = (1, 4, 24), pip: float = 0.01) -> dict:
    """判断のあと、値段がどう動いたか（pips）。見送った判断が正しかったかを振り返るために使う。

    ``bars`` は仲値の1分足。返すのは、時間ごとの終値の変化と、その間に上下へ最大どれだけ動いたか。
    データが足りない時間は含めない。
    """
    after = bars[bars.index >= at]
    if after.empty:
        return {}
    start = float(after["open"].iloc[0])
    out: dict[str, float] = {}
    for h in hours:
        window = after[after.index < at + pd.Timedelta(hours=h)]
        if window.empty or window.index[-1] < at + pd.Timedelta(hours=h) - pd.Timedelta(minutes=5):
            continue
        out[f"change_{h}h"] = round((float(window["close"].iloc[-1]) - start) / pip, 1)
        out[f"up_{h}h"] = round((float(window["high"].max()) - start) / pip, 1)
        out[f"down_{h}h"] = round((start - float(window["low"].min())) / pip, 1)
    return out
