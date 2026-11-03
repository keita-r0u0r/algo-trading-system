"""経済指標の予定（I/O なし）。発表の時刻を、AI の材料と短期売買の休みに使う。

予定そのものは ``infrastructure/econ_calendar.py`` が公開の週間カレンダーから取ってくる（重要度・予想・前回つき）。
ここでは、その予定から次を決める。

  * ``upcoming``: AI に渡す「この先・直前の発表」（建てる・持つ判断の材料）
  * ``quiet_reason``: 重要な発表の前後で、短期売買が新しく建てない理由（発表直後は値段が飛び、損切りがずれやすい）
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable

JST = dt.timezone(dt.timedelta(hours=9))
IMPACT_LABELS = {"High": "重要", "Medium": "中"}
#: 重要な発表の前後で、短期売買が新しく建てない時間（分）
QUIET_BEFORE_MINUTES = 10
QUIET_AFTER_MINUTES = 15


@dataclass(frozen=True)
class EconEvent:
    at: dt.datetime
    currency: str
    title: str
    impact: str
    forecast: str = ""
    previous: str = ""


def parse(raw: Iterable[dict[str, Any]]) -> list[EconEvent]:
    """カレンダーの行（date・country・title・impact・forecast・previous）を読む。重要度が中以上のものだけ。"""
    events = []
    for r in raw:
        if r.get("impact") not in IMPACT_LABELS:
            continue
        try:
            at = dt.datetime.fromisoformat(str(r["date"]))
        except (KeyError, ValueError):
            continue
        if at.tzinfo is None:
            continue
        events.append(EconEvent(at, str(r.get("country", "")), str(r.get("title", "")), str(r["impact"]),
                                str(r.get("forecast") or ""), str(r.get("previous") or "")))
    return sorted(events, key=lambda e: e.at)


def currencies(symbols: Iterable[str]) -> set[str]:
    return {c for s in symbols for c in s.split("_")}


def upcoming(events: Iterable[EconEvent], now: dt.datetime, symbols: Iterable[str], *,
             ahead: dt.timedelta = dt.timedelta(hours=12), behind: dt.timedelta = dt.timedelta(hours=2)
             ) -> list[dict[str, Any]]:
    """``now`` の ``behind`` 前から ``ahead`` 先までの、通貨ペアに関係する発表（AI に渡す形）。"""
    wanted = currencies(symbols)
    rows = []
    for e in events:
        if e.currency not in wanted or not now - behind <= e.at <= now + ahead:
            continue
        minutes = round((e.at - now).total_seconds() / 60)
        rows.append({"at_jst": e.at.astimezone(JST).strftime("%m-%d %H:%M"),
                     "minutes_from_now": minutes, "when": _when(minutes),
                     "currency": e.currency, "title": e.title, "impact": IMPACT_LABELS[e.impact],
                     "forecast": e.forecast, "previous": e.previous})
    return rows


def _when(minutes: int) -> str:
    """「あと2時間22分」「15分前に発表済み」。AI が時刻を読み違えないように、文でも渡す。"""
    span = abs(minutes)
    text = f"{span // 60}時間{span % 60}分" if span >= 60 else f"{span}分"
    return f"あと{text}" if minutes >= 0 else f"{text}前に発表済み"


def quiet_reason(events: Iterable[EconEvent], symbol: str, at: dt.datetime) -> str | None:
    """``symbol`` に関係する重要な発表の前後なら、その理由（新しく建てない）。無ければ None。"""
    wanted = currencies([symbol])
    for e in events:
        if e.impact != "High" or e.currency not in wanted:
            continue
        minutes = (e.at - at).total_seconds() / 60
        if -QUIET_AFTER_MINUTES <= minutes <= QUIET_BEFORE_MINUTES:
            when = f"{minutes:.0f}分前" if minutes >= 0 else f"{-minutes:.0f}分後"
            return f"{e.currency} の重要な発表「{e.title}」（{e.at.astimezone(JST):%H:%M}）の{when}"
    return None
