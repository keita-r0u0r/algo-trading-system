"""数量の上限の見直しを AI に提案させるための材料と、提案の確かめ（I/O なし）。

AI は1日1回、判断する者ごとの実際の成績（勝ちの大きさ・負けの大きさ・連敗・資金に対する損）を見て、
数量の上限（``max_units``）とリスクの上限（``max_risk``）の見直しを提案する。

**提案は自動では効かない。** 画面に出て、利用者が「適用」を押したときだけ設定（``fx_filter.FxSettings.sizing``）が変わる。
提案は全体の枠（1口座1,000通貨・1回の損切りで資金の5%）の内側に収めてから出す。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Iterable

from . import fx_filter, fx_setups

#: 前回の提案からこれだけたつまで、新しく提案させない
PROPOSE_EVERY = dt.timedelta(hours=20)
#: 提案させる最初の時刻（日本時間。朝の営業日の始まりのあと）
PROPOSE_FROM_HOUR = 7


def decider_stats(events: Iterable[dict[str, Any]], account: str, equity: float) -> dict[str, Any]:
    """判断する者の実際の成績（円）。直近20回と全体。"""
    trades = fx_setups.closed_trades(events, account)
    results = [t["pnl"] for t in trades]

    def summary(xs: list[float]) -> dict[str, Any]:
        wins, losses = [x for x in xs if x > 0], [x for x in xs if x <= 0]
        streak = worst_streak = 0
        for x in xs:
            streak = streak + 1 if x <= 0 else 0
            worst_streak = max(worst_streak, streak)
        peak = cum = drawdown = 0.0
        for x in xs:
            cum += x
            peak = max(peak, cum)
            drawdown = max(drawdown, peak - cum)
        return {"trades": len(xs), "total_yen": round(sum(xs), 1),
                "win_rate": round(len(wins) / len(xs), 2) if xs else None,
                "avg_win_yen": round(sum(wins) / len(wins), 1) if wins else None,
                "avg_loss_yen": round(sum(losses) / len(losses), 1) if losses else None,
                "worst_losing_streak": worst_streak, "max_drawdown_yen": round(drawdown, 1),
                "max_drawdown_of_equity": round(drawdown / equity, 3) if equity else None}

    return {"all": summary(results), "last_20": summary(results[-20:]),
            "last_trades": [{"symbol": t["symbol"], "side": "買い" if t["side"] > 0 else "売り", "pnl_yen": t["pnl"],
                             "closed_at": t["closed_at"][5:16]} for t in trades[-10:]]}


def sizing_context(events: list[dict[str, Any]], settings: fx_filter.FxSettings, *, equity: float,
                   available: float | None, now: dt.datetime, accounts: Iterable[str]) -> dict[str, Any]:
    return {
        "now_jst": now.strftime("%Y-%m-%d %H:%M"),
        "goal": "利用者の目標は1か月で資金1.5倍。ただし致命的な損（入金額を超える損）は避ける",
        "equity_yen": round(equity), "available_margin_yen": round(available) if available is not None else None,
        "limits": {"max_units_range": list(fx_filter.UNITS_RANGE), "max_risk_range": list(fx_filter.RISK_RANGE),
                   "note": "数量は100通貨単位。全部の判断する者の建玉の証拠金の合計が、取引余力を超えると建てられない"
                           "（1,000通貨のドル円で約6,300円）"},
        "deciders": {name: {"current": {"max_units": settings.sizing_for(name).max_units,
                                        "max_risk": settings.sizing_for(name).max_risk},
                            "stage": settings.stage(name),
                            "stats": decider_stats(events, name, equity)} for name in accounts},
    }


def checked_changes(data: dict[str, Any], settings: fx_filter.FxSettings) -> list[dict[str, Any]]:
    """AI の提案を、枠の内側に収め、今と同じものは落とす。"""
    changes = []
    for c in data.get("changes") or []:
        name = c.get("account")
        if name not in fx_filter.DEFAULT_SIZING:
            continue
        now = settings.sizing_for(name)
        new = fx_filter.clamp_sizing(c.get("max_units", now.max_units), c.get("max_risk", now.max_risk), now)
        if new == now:
            continue
        changes.append({"account": name, "from": {"max_units": now.max_units, "max_risk": now.max_risk},
                        "to": {"max_units": new.max_units, "max_risk": new.max_risk},
                        "reason": str(c.get("reason", ""))[:400]})
    return changes


def proposal_record(data: dict[str, Any], settings: fx_filter.FxSettings, now: dt.datetime, model: str,
                    trigger: str) -> dict[str, Any]:
    return {"kind": "proposal", "id": uuid.uuid4().hex[:8], "at": now.isoformat(timespec="seconds"), "model": model,
            "trigger": trigger, "summary": str(data.get("summary", ""))[:600],
            "changes": checked_changes(data, settings)}


def due(proposals: list[dict[str, Any]], now: dt.datetime) -> bool:
    """1日1回（朝7時以降、前回から PROPOSE_EVERY たったら）。"""
    if now.hour < PROPOSE_FROM_HOUR or now.weekday() >= 5:
        return False
    last = proposals[-1] if proposals else None  # 失敗した回も数える（失敗が続いても5分ごとに呼ばない）
    return last is None or now - dt.datetime.fromisoformat(last["at"]) >= PROPOSE_EVERY


def pending(proposals: list[dict[str, Any]], answers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """まだ答えていない、いちばん新しい提案（変えるものがあるときだけ）。"""
    answered = {a.get("id") for a in answers}
    for p in reversed(proposals):
        if p.get("kind") == "proposal" and p.get("changes"):
            return None if p["id"] in answered else p
    return None


def apply(settings: fx_filter.FxSettings, proposal: dict[str, Any], now: dt.datetime) -> fx_filter.FxSettings:
    """提案を設定に当てはめる（枠の内側に収め直す）。"""
    sizing = dict(settings.sizing)
    for c in proposal.get("changes") or []:
        default = settings.sizing_for(c["account"])
        sizing[c["account"]] = fx_filter.clamp_sizing(c["to"]["max_units"], c["to"]["max_risk"], default)
    return fx_filter.FxSettings(settings.deciders, settings.filter, now.isoformat(timespec="seconds"),
                                settings.exits, sizing, settings.evaluate)
