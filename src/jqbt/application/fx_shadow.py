"""記録だけ（shadow）の段階の判断する者の、仮の建玉と結果（I/O なし）。

段階が ``shadow`` の判断する者は、実際の口座と同じ材料・同じ枠で判断する。通った判断は注文を出さずに、
ここの仮の建玉として記録する（``state/fx_shadow.jsonl``）。建てた値段は買いなら買値、売りなら売値。
損切り・利確は、回ごとに、前の回からの1分足の高値・安値で当たったかを確かめる（不利な側を先に見る）。

成績は pips と、損切り幅の何倍か（R）で見る。本番に上げるかは、この成績と画面の比べで決める（docs/release.md）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

PIP = 0.01


@dataclass(frozen=True)
class ShadowPosition:
    account: str
    symbol: str
    side: int
    units: float
    price: float
    stop: float | None
    take: float | None
    opened_at: str


def book(events: Iterable[dict[str, Any]]) -> dict[str, ShadowPosition]:
    """記録から、判断する者ごとの仮の建玉。"""
    positions: dict[str, ShadowPosition] = {}
    for e in events:
        if e.get("kind") == "shadow_open":
            positions[e["account"]] = ShadowPosition(e["account"], e["symbol"], int(e["side"]), float(e["units"]),
                                                     float(e["price"]), e.get("stop"), e.get("take"), e["at"])
        elif e.get("kind") == "shadow_close":
            positions.pop(e.get("account"), None)
    return positions


def open_event(account: str, symbol: str, units: float, stop_pips: float | None, take_pips: float | None,
               quote: dict[str, float], now: dt.datetime, decision: dict[str, Any]) -> dict[str, Any]:
    side = 1 if units > 0 else -1
    price = quote["ask"] if side > 0 else quote["bid"]
    return {"kind": "shadow_open", "at": now.isoformat(timespec="seconds"), "account": account, "symbol": symbol,
            "side": side, "units": abs(units), "price": price,
            "stop": round(price - side * stop_pips * PIP, 3) if stop_pips else None,
            "take": round(price + side * take_pips * PIP, 3) if take_pips else None,
            "decision": {k: v for k, v in decision.items() if k not in ("context",)}}


def close_event(p: ShadowPosition, price: float, now: dt.datetime | str, reason: str) -> dict[str, Any]:
    pips = round(p.side * (price - p.price) / PIP, 1)
    stop_pips = abs(p.price - p.stop) / PIP if p.stop else None
    return {"kind": "shadow_close", "at": now if isinstance(now, str) else now.isoformat(timespec="seconds"),
            "account": p.account, "symbol": p.symbol, "side": p.side, "opened_at": p.opened_at, "price": price,
            "pips": pips, "yen": round(pips * PIP * p.units, 1), "r": round(pips / stop_pips, 2) if stop_pips else None,
            "reason": reason}


def stop_or_take(p: ShadowPosition, bars: pd.DataFrame | None) -> dict[str, Any] | None:
    """建ててから今までの1分足で、損切り・利確に当たっていれば、その決済の記録。"""
    if bars is None or bars.empty:
        return None
    since = bars[bars.index >= pd.Timestamp(dt.datetime.fromisoformat(p.opened_at).replace(tzinfo=None)).floor("min")]
    for ts, r in since.iterrows():
        adverse, favorable = (r["low"], r["high"]) if p.side > 0 else (r["high"], r["low"])
        if p.stop is not None and p.side * (adverse - p.stop) <= 0:
            return close_event(p, p.stop, ts.isoformat() + "+09:00", "損切り")
        if p.take is not None and p.side * (favorable - p.take) >= 0:
            return close_event(p, p.take, ts.isoformat() + "+09:00", "利確")
    return None


def results(events: Iterable[dict[str, Any]], account: str) -> dict[str, Any]:
    closes = [e for e in events if e.get("kind") == "shadow_close" and e.get("account") == account]
    pips = [e["pips"] for e in closes]
    wins, losses = [x for x in pips if x > 0], [x for x in pips if x <= 0]
    return {"trades": len(pips), "total_pips": round(sum(pips), 1),
            "total_r": round(sum(e["r"] for e in closes if e.get("r") is not None), 2),
            "win_rate": round(len(wins) / len(pips), 2) if pips else None,
            "payoff": round((sum(wins) / len(wins)) / -(sum(losses) / len(losses)), 2) if wins and losses and sum(losses) else None}
