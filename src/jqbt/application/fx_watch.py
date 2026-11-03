"""値段の常時の見張り（I/O なし）。機械的な条件に当たったときだけ、判断の回（AI を含む）を起こす。

AI を毎回呼ぶのは高く遅いので、見張りは機械的に行い、何かが起きたときだけ判断させる。

## 起こす条件（どれか1つ）

  * 急変: ある通貨ペアが ``window_minutes`` 分で ``move_pips`` 以上動いた
  * 建玉が損切り・利確に近づいた: ペーパー運用の建玉で、あと ``near_pips`` 以内
  * 市場の時間帯の切り替わり: ``session_times``（日本時間。ロンドン・ニューヨークの始まり）

## 起こさないとき（判断の回が重なると、記録が壊れるため）

  * 直前に起こしてから ``cooldown_minutes`` 分たっていない
  * 定期の回（5分ごと）の前後 ``scheduled_guard_minutes`` 分
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class WatchConfig:
    move_pips: float = 15.0
    window_minutes: int = 5
    near_pips: float = 3.0
    #: 冬時間（11月〜3月）の目安。ロンドン 17:00、ニューヨーク 22:00
    session_times: tuple[str, ...] = ("17:00", "22:00")
    cooldown_minutes: int = 5
    #: 定期の回の間隔と、その前後で起こさない時間（定期の回と重ならないように）
    scheduled_every_minutes: int = 5
    scheduled_guard_minutes: int = 1
    pip: float = 0.01


@dataclass
class PriceHistory:
    """通貨ペアごとの直近の仲値（時刻つき）。``window_minutes`` より古いものは捨てる。"""

    window: dt.timedelta
    points: dict[str, deque] = field(default_factory=dict)

    def add(self, symbol: str, at: dt.datetime, mid: float) -> None:
        q = self.points.setdefault(symbol, deque())
        q.append((at, mid))
        while q and at - q[0][0] > self.window:
            q.popleft()

    def move_pips(self, symbol: str, pip: float) -> float:
        """窓の中の一番古い値から今までの動き（pips、符号つき）。"""
        q = self.points.get(symbol)
        if not q or len(q) < 2:
            return 0.0
        return (q[-1][1] - q[0][1]) / pip

    def last(self, symbol: str) -> float | None:
        q = self.points.get(symbol)
        return q[-1][1] if q else None


def reason_to_wake(
    now: dt.datetime,
    history: PriceHistory,
    positions: list[dict],
    config: WatchConfig,
    *,
    last_wake: dt.datetime | None,
    fired_sessions: set[str],
) -> str:
    """判断の回を起こす理由（無ければ空）。``now`` は日本時間。"""
    if last_wake and now - last_wake < dt.timedelta(minutes=config.cooldown_minutes):
        return ""
    minute = now.minute % config.scheduled_every_minutes
    if minute < config.scheduled_guard_minutes or minute >= config.scheduled_every_minutes - config.scheduled_guard_minutes:
        return ""  # 定期の回が近い
    for symbol in history.points:
        move = history.move_pips(symbol, config.pip)
        if abs(move) >= config.move_pips:
            return f"急変: {symbol} が {config.window_minutes}分で {move:+.1f} pips"
    for p in positions:
        price = history.last(p["symbol"])
        if price is None:
            continue
        for label, level in (("損切り", p.get("stop")), ("利確", p.get("take"))):
            if level is not None and abs(price - level) / config.pip <= config.near_pips:
                return f"{p['account']} の建玉が{label}（{level}）まで {abs(price - level) / config.pip:.1f} pips"
    key = f"{now:%Y-%m-%d} "
    for hhmm in config.session_times:
        if f"{now:%H:%M}" >= hhmm and key + hhmm not in fired_sessions and now.weekday() < 5:
            start = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
            if now - start < dt.timedelta(minutes=10):
                fired_sessions.add(key + hhmm)
                return f"時間帯の切り替わり（{hhmm}）"
    return ""


def watch_snapshot(now: dt.datetime, history: PriceHistory, positions: list[dict], config: WatchConfig, *,
                   last_wake: dt.datetime | None, fired_sessions: set[str]) -> dict:
    """今なにを見張っていて、判断を起こす線まであとどれくらいか（画面に出す）。``reason_to_wake`` と同じ線を使う。"""
    symbols = {}
    for symbol in sorted(history.points):
        move = history.move_pips(symbol, config.pip)
        symbols[symbol] = {"price": history.last(symbol), "move_pips": round(move, 1),
                           "to_wake_pips": round(max(0.0, config.move_pips - abs(move)), 1)}
    watched = []
    for p in positions:
        price = history.last(p["symbol"])
        row = {"venue": p.get("venue"), "account": p.get("account"), "symbol": p["symbol"],
               "stop": p.get("stop"), "take": p.get("take")}
        if price is not None:
            for key, level in (("pips_to_stop", p.get("stop")), ("pips_to_take", p.get("take"))):
                if level is not None:
                    row[key] = round(abs(price - level) / config.pip, 1)
        watched.append(row)
    upcoming = [h for h in config.session_times if f"{now:%H:%M}" < h and f"{now:%Y-%m-%d} {h}" not in fired_sessions]
    cooldown_until = last_wake + dt.timedelta(minutes=config.cooldown_minutes) if last_wake else None
    return {
        "at": now.isoformat(timespec="seconds"),
        "rules": {"move_pips": config.move_pips, "window_minutes": config.window_minutes, "near_pips": config.near_pips,
                  "session_times": list(config.session_times), "cooldown_minutes": config.cooldown_minutes},
        "symbols": symbols, "positions": watched,
        "next_session": upcoming[0] if upcoming and now.weekday() < 5 else None,
        "cooldown_until": cooldown_until.isoformat(timespec="seconds") if cooldown_until and cooldown_until > now else None,
    }
