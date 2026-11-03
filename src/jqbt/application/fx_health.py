"""FX の自動売買の健康診断（I/O なし）。動いているはずのものが、本当に動いているかを機械的に確かめる。

「持ち続ける」が並んでいても、AI が実際に考えた結果なのか、呼べなかったときの既定の答えなのかは、
記録の中身を見ないとわからない。ここでは、記録（見張り役の生きている印・実際の口座の記録）から、
次を確かめる。どれも、画面（概要の上）と反映後の確認（``scripts/fx_verify.py``）が同じものを使う。

  * 見張り役: 値段を受け取っているか・判断の回が実際の口座に届いているか・売買を担当しているか
  * 判断する者: 判断の回が回っているか・AI が実際に呼べているか・ロジックが判断できているか（``decider_health``）
  * 口座: 直近の照会・発注の失敗、キルスイッチ
  * ペーパー運用: 止められていないか（止めていても実際の売買には関係ないので、知らせるだけ）

市場が閉まっている間（土曜 7:00〜月曜 7:00）は、動いていないのが正しいので、止まっているとは言わない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Sequence

JST = dt.timezone(dt.timedelta(hours=9))
#: AI は15分ごと（見張り役が起こした回は必ず）に呼ぶので、20分を超えて実際に呼べていなければおかしい。
#: 判断の回そのものは5分ごとなので、15分を超えて無ければ止まっている
AI_STALE_MINUTES = 20
DECISION_STALE_MINUTES = 15
TICK_STALE_MINUTES = 3
ERROR_WINDOW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class Finding:
    """1つの確認の結果。state は ok / info / warn / bad。"""

    name: str
    state: str
    text: str


def market_open(now: dt.datetime) -> bool:
    """GMOコインの FX が開いているか（土曜 7:00〜月曜 7:00 は閉まっている。冬時間の目安）。"""
    now = now.astimezone(JST)
    weekday, hour = now.weekday(), now.hour
    if weekday == 5 and hour >= 7 or weekday == 6 or weekday == 0 and hour < 7:
        return False
    return True


def _at(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def decider_health(events: Sequence[dict[str, Any]], name: str, now: dt.datetime, *,
                   market_open: bool = True) -> dict[str, Any]:
    """判断する者 ``name`` の動き具合（実際の口座の判断の記録 live_decision から）。

    返すのは 状態（ok / warn / bad / info）・その説明・最後に実際に判断した時刻・直近1時間の内訳・最後の失敗。
    """
    mine = [e for e in events if e.get("kind") == "live_decision" and e.get("account") == name]
    hour_ago = now - ERROR_WINDOW
    recent = [e for e in mine if (t := _at(e.get("at"))) and t >= hour_ago]
    is_ai = name == "ai"
    real = [e for e in mine if (e.get("model") if is_ai else e.get("logic"))]
    failed = [e for e in mine if e.get("error")]
    last = _at(mine[-1].get("at")) if mine else None
    last_real = _at(real[-1].get("at")) if real else None
    result = {
        "last": last, "last_real": last_real,
        "real_1h": sum(1 for e in recent if (e.get("model") if is_ai else e.get("logic"))),
        "failed_1h": sum(1 for e in recent if e.get("error")),
        "rounds_1h": len(recent), "last_error": failed[-1].get("error") if failed else "",
    }
    if not market_open:
        return {**result, "state": "info", "text": "市場が閉まっている"}
    if last is None or now - last > dt.timedelta(minutes=DECISION_STALE_MINUTES):
        return {**result, "state": "bad", "text": "判断の回が止まっている"}
    if last_real is None or now - last_real > dt.timedelta(minutes=AI_STALE_MINUTES if is_ai else DECISION_STALE_MINUTES):
        what = "AI を呼べていない" if is_ai else "ロジックが判断できていない"
        reason = f"（{str(result['last_error'])[:80]}）" if result["last_error"] else ""
        return {**result, "state": "bad", "text": what + reason}
    if result["failed_1h"]:
        return {**result, "state": "warn", "text": f"直近1時間に{result['failed_1h']}回失敗"}
    return {**result, "state": "ok", "text": "動いている"}


def check_all(now: dt.datetime, *, watch: dict[str, Any], live_events: Sequence[dict[str, Any]],
              accounts: Sequence[str], live_enabled: bool, killed: bool, paper_paused: bool) -> list[Finding]:
    """全部の確認。悪い順（bad → warn → info → ok）に並べて返す。"""
    is_open = market_open(now)
    findings: list[Finding] = []

    # 見張り役
    tick, ok_cycle = _at(watch.get("last_tick")), _at(watch.get("last_ok_cycle"))
    if not watch:
        findings.append(Finding("見張り役", "bad", "生きている印が無い（動いていない）"))
    elif not is_open:
        findings.append(Finding("見張り役", "info", "市場が閉まっている"))
    elif tick is None or now - tick > dt.timedelta(minutes=TICK_STALE_MINUTES):
        findings.append(Finding("見張り役", "bad", f"値段が届いていない（最後 {str(watch.get('last_tick'))[5:16]}）"))
    elif not watch.get("trading"):
        findings.append(Finding("見張り役", "warn", "見張るだけで、売買を担当していない（ジョブが代わりに売買）"))
    elif ok_cycle is None or now - ok_cycle > dt.timedelta(minutes=DECISION_STALE_MINUTES):
        findings.append(Finding("見張り役", "bad", "判断の回が実際の口座に届いていない（キー・IP の制限など）"))
    else:
        findings.append(Finding("見張り役", "ok", f"動いている（版 {watch.get('version') or '?'}・{watch.get('cycles', 0)}回）"))

    # 判断する者
    for name in accounts:
        h = decider_health(live_events, name, now, market_open=is_open)
        findings.append(Finding(name, h["state"], h["text"]))

    # 口座
    if not live_enabled:
        findings.append(Finding("自動売買", "warn", "有効になっていない（fx_live_enabled）"))
    elif killed:
        findings.append(Finding("自動売買", "bad", "キルスイッチが入っている（新しく建てない）"))
    ready = next((e for e in reversed(live_events) if e.get("kind") == "live_ready"), None)
    if ready is not None and not ready.get("ok"):
        findings.append(Finding("口座", "bad", f"照会できない（{str(ready.get('note', ''))[:80]}）"))
    errors = [e for e in live_events if e.get("kind") in ("live_error", "error")
              and (t := _at(e.get("at"))) and now - t <= ERROR_WINDOW]
    if errors:
        last = errors[-1]
        text = str(last.get("error") or last.get("errors") or "")[:80]
        findings.append(Finding("口座", "warn", f"直近1時間に{len(errors)}回エラー（最後: {text}）"))

    if paper_paused:
        findings.append(Finding("ペーパー運用", "info", "止めている（実際の売買には関係ない）"))

    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    return sorted(findings, key=lambda f: order.get(f.state, 9))


def worst(findings: Sequence[Finding]) -> str:
    """いちばん悪い状態。"""
    for state in ("bad", "warn", "info"):
        if any(f.state == state for f in findings):
            return state
    return "ok"
