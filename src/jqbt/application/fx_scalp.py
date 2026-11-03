"""短期売買（スキャルピング）の **ペーパー運用**（I/O なし）。実際の注文は出さない。

見張り役が受け取る値段（1秒に約10回）の1つ1つで判断し、仮想で約定する。長期の判断する者（AI・金利差＋流れ）とは
別の口座で動き、うまくいきそうなら実運用に展開するかを決めるための検証。

## 決まり（RULES。変えたら SCALP_VERSION を上げる）

どちらも「``trigger_seconds`` 秒で ``trigger_pips`` 以上の急な動き」をきっかけにする。考え方が反対の2つを並べて比べる。

  * 逆張り（scalp_fade）: 急な動きの **反対** に建てる。行き過ぎの戻りを取る
  * 順張り（scalp_follow）: 急な動きと **同じ向き** に建てる。勢いに乗る

## 約定の決まり（実運用と同じにする）

  * 買いは ask、売りは bid で建て、閉じるときはその反対（スプレッドを払う）。手数料は0円（実際の約定記録の fee が0）。
    scalp-v1 は誤って 0.002%（1,000通貨で往復 約4.4円）を引いていたので、成績ではその分を戻して数える
  * 損切り・利確は、値段が届いたその値段で約定したことにする（実際はずれることがある。甘めの見積もり）
  * スプレッドが ``max_spread_pips`` を超えるときは建てない。建玉は口座ごとに1つ。閉じたあと ``cooldown_seconds`` は建てない
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from typing import Any

SCALP_VERSION = "scalp-v2"
FEE_RATE = 0.0


@dataclass(frozen=True)
class ScalpRule:
    name: str
    #: "fade"（反対に建てる）か "follow"（同じ向きに建てる）
    mode: str
    trigger_pips: float = 5.0
    trigger_seconds: int = 60
    take_pips: float = 3.0
    stop_pips: float = 5.0
    max_hold_seconds: int = 600
    cooldown_seconds: int = 120
    max_spread_pips: float = 1.0
    units: int = 1_000
    pip: float = 0.01

    def describe(self) -> str:
        side = "反対" if self.mode == "fade" else "同じ向き"
        return (f"{self.trigger_seconds}秒で {self.trigger_pips:g} pips 以上動いたら、{side}に {self.units:,}通貨建てる。"
                f"利確 {self.take_pips:g} pips・損切り {self.stop_pips:g} pips・最長 {self.max_hold_seconds // 60}分。"
                f"スプレッドが {self.max_spread_pips:g} pips を超えるときは建てない")


RULES = (
    ScalpRule("scalp_fade", "fade", take_pips=3.0, stop_pips=5.0),
    ScalpRule("scalp_follow", "follow", take_pips=5.0, stop_pips=3.0),
)
INITIAL = 10_000.0


@dataclass
class ScalpPosition:
    symbol: str
    side: int  # 1 買い / -1 売り
    price: float
    opened_at: dt.datetime
    stop: float
    take: float
    trigger: str


@dataclass
class ScalpAccount:
    rule: ScalpRule
    cash: float = INITIAL
    position: ScalpPosition | None = None
    last_closed_at: dt.datetime | None = None
    trades: int = 0
    wins: int = 0


@dataclass
class TickWindow:
    """通貨ペアごとの直近の仲値（時刻つき）。``seconds`` より古いものは捨てる。"""

    seconds: int
    points: dict[str, deque] = field(default_factory=dict)

    def add(self, symbol: str, at: dt.datetime, mid: float) -> None:
        q = self.points.setdefault(symbol, deque())
        q.append((at, mid))
        while q and (at - q[0][0]).total_seconds() > self.seconds:
            q.popleft()

    def move(self, symbol: str) -> float:
        q = self.points.get(symbol)
        return q[-1][1] - q[0][1] if q and len(q) >= 2 else 0.0


def _close(account: ScalpAccount, at: dt.datetime, price: float, reason: str) -> dict[str, Any]:
    p = account.position
    assert p is not None
    units = account.rule.units
    gross = p.side * (price - p.price) * units
    fee = (p.price + price) * units * FEE_RATE
    pnl = gross - fee
    account.cash += pnl
    account.trades += 1
    account.wins += int(pnl > 0)
    account.position, account.last_closed_at = None, at
    return {"kind": "scalp_close", "at": at.isoformat(timespec="seconds"), "account": account.rule.name,
            "symbol": p.symbol, "side": "BUY" if p.side > 0 else "SELL", "units": units,
            "entry": p.price, "exit": price, "pips": round(p.side * (price - p.price) / account.rule.pip, 1),
            "pnl": round(pnl, 2), "fee": round(fee, 2), "reason": reason,
            "held_seconds": int((at - p.opened_at).total_seconds()), "cash": round(account.cash, 2),
            "version": SCALP_VERSION}


def on_tick(account: ScalpAccount, window: TickWindow, symbol: str, at: dt.datetime, bid: float,
            ask: float) -> list[dict[str, Any]]:
    """値段が1つ届いたときの判断と仮想の約定。``window`` には、この値段をもう足してあること。記録する出来事を返す。"""
    rule, events = account.rule, []
    p = account.position
    if p is not None:
        if p.symbol != symbol:
            return events
        exit_price = bid if p.side > 0 else ask
        hit_stop = exit_price <= p.stop if p.side > 0 else exit_price >= p.stop
        hit_take = exit_price >= p.take if p.side > 0 else exit_price <= p.take
        if hit_stop:
            events.append(_close(account, at, p.stop, "損切り"))
        elif hit_take:
            events.append(_close(account, at, p.take, "利確"))
        elif (at - p.opened_at).total_seconds() >= rule.max_hold_seconds:
            events.append(_close(account, at, exit_price, f"{rule.max_hold_seconds // 60}分たった"))
        return events
    if account.last_closed_at and (at - account.last_closed_at).total_seconds() < rule.cooldown_seconds:
        return events
    move_pips = window.move(symbol) / rule.pip
    if abs(move_pips) < rule.trigger_pips:
        return events
    spread = (ask - bid) / rule.pip
    if spread > rule.max_spread_pips:
        return events
    direction = 1 if move_pips > 0 else -1
    side = -direction if rule.mode == "fade" else direction
    price = ask if side > 0 else bid
    trigger = f"{rule.trigger_seconds}秒で {move_pips:+.1f} pips"
    account.position = ScalpPosition(symbol, side, price, at, round(price - side * rule.stop_pips * rule.pip, 3),
                                     round(price + side * rule.take_pips * rule.pip, 3), trigger)
    events.append({"kind": "scalp_open", "at": at.isoformat(timespec="seconds"), "account": rule.name,
                   "symbol": symbol, "side": "BUY" if side > 0 else "SELL", "units": rule.units, "price": price,
                   "spread_pips": round(spread, 2), "stop": account.position.stop, "take": account.position.take,
                   "trigger": trigger, "version": SCALP_VERSION})
    return events


def _pnl(e: dict[str, Any]) -> float:
    """閉じた記録の損益。scalp-v1 で誤って引いた手数料は戻す。"""
    pnl = float(e.get("pnl", 0))
    return pnl + float(e.get("fee", 0)) if e.get("version") == "scalp-v1" else pnl


def restore(rule: ScalpRule, events: list[dict[str, Any]]) -> ScalpAccount:
    """記録から口座を作り直す（見張り役が作り直されても続きから）。閉じていない建玉は、作り直しのときに捨てる。"""
    account = ScalpAccount(rule)
    for e in events:
        if e.get("account") != rule.name or e.get("kind") != "scalp_close":
            continue
        account.cash += _pnl(e)
        account.trades += 1
        account.wins += int(_pnl(e) > 0)
    return account


def summary(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """成績（画面に出す）。"""
    closes = [e for e in events if e.get("kind") == "scalp_close" and e.get("account") == name]
    pnl = [_pnl(e) for e in closes]
    pips = [float(e["pips"]) for e in closes]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    return {
        "trades": len(closes),
        "pnl": round(sum(pnl), 1),
        "equity": round(INITIAL + sum(pnl), 1),
        "win_rate": len(wins) / len(closes) if closes else None,
        "avg_pips": round(sum(pips) / len(pips), 2) if pips else None,
        "avg_win": round(sum(wins) / len(wins), 1) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 1) if losses else None,
        "fees": round(sum(float(e.get("fee", 0)) for e in closes if e.get("version") != "scalp-v1"), 1),
        "by_reason": {r: sum(1 for e in closes if e.get("reason") == r) for r in sorted({e.get("reason") for e in closes})},
    }
