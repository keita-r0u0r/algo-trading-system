"""FX の自動売買の健康診断（I/O なし）。動いているはずのものが、本当に動いているかを機械的に確かめる。

「持ち続ける」が並んでいても、AI が実際に考えた結果なのか、呼べなかったときの既定の答えなのかは、
記録の中身を見ないとわからない。ここでは、記録（見張り役の生きている印・実際の口座の記録）から、
次を確かめる。どれも、画面（概要の上）と反映後の確認（``scripts/fx_verify.py``）が同じものを使う。

  * 見張り役: 値段を受け取っているか・判断の回が実際の口座に届いているか・売買を担当しているか
  * 判断する者: 判断の回が回っているか・AI が実際に呼べているか・ロジックが判断できているか（``decider_health``）
  * 口座: 直近の照会・発注の失敗、キルスイッチ
  * ペーパー運用: 止められていないか（止めていても実際の売買には関係ないので、知らせるだけ）
  * 経済指標の予定: 取れているか（1時間ごとに取り直す。取れなくても売買は止めないので、注意にとどめる）

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
#: 値段が途切れてもこの分数までは、作り直し（反映）や一時的な切断として「注意」にとどめる。超えたら異常
TICK_DOWN_MINUTES = 10
ERROR_WINDOW = dt.timedelta(hours=1)
#: 経済指標の予定は1時間ごとに取り直す。3時間を超えて新しくならなければ、取れていない
CALENDAR_STALE = dt.timedelta(hours=3)


@dataclass(frozen=True)
class Finding:
    """1つの確認の結果。state は ok / info / warn / bad。

    text は何が起きているか、impact はそれで何が困るか（困らないか）、action は利用者がやること。
    """

    name: str
    state: str
    text: str
    impact: str = ""
    action: str = ""


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
              accounts: Sequence[str], live_enabled: bool, killed: bool, paper_paused: bool,
              calendar: dict[str, Any] | None = None) -> list[Finding]:
    """全部の確認。悪い順（bad → warn → info → ok）に並べて返す。"""
    is_open = market_open(now)
    findings: list[Finding] = []

    # 見張り役
    tick, ok_cycle, started = _at(watch.get("last_tick")), _at(watch.get("last_ok_cycle")), _at(watch.get("started_at"))
    covered = "その間は、5分ごとのジョブが代わりに判断する。建玉は GMOコイン側の損切り・利確で守られている"
    if not watch:
        findings.append(Finding("見張り役", "bad", "生きている印が無い（一度も動いていない）",
                                "急変への随時判断ができない。" + covered,
                                "fx_watch_enabled = true で反映したか確かめる。わからなければ Claude に「見張り役が動いていない」と伝える"))
    elif not is_open:
        findings.append(Finding("見張り役", "info", "市場が閉まっている（土曜 7:00〜月曜 7:00）", "なし", "なし"))
    elif tick is None or now - tick > dt.timedelta(minutes=TICK_STALE_MINUTES):
        minutes = (now - tick).total_seconds() / 60 if tick else None
        since = f"{tick:%H:%M} から {minutes:.0f}分" if tick else "起動してから"
        if minutes is not None and minutes <= TICK_DOWN_MINUTES:
            findings.append(Finding("見張り役", "warn", f"値段が届いていない（{since}）。反映による作り直しか、一時的な切断の可能性が高い",
                                    "急変への随時判断が止まっている。" + covered,
                                    f"反映した直後なら待つだけでよい（数分で戻る）。{TICK_DOWN_MINUTES}分を超えたら異常として出す"))
        else:
            findings.append(Finding("見張り役", "bad", f"値段が届いていない（{since}）",
                                    "急変への随時判断が止まっている。" + covered,
                                    "Claude に「見張り役が止まっている」と伝える（仮想マシンの再起動・ログの確認をする）"))
    elif not watch.get("trading"):
        findings.append(Finding("見張り役", "warn", "見張るだけで、実際の売買を担当していない",
                                "急変への随時判断が実際の口座に届かない。" + covered,
                                "FX_WATCH_TRADES が true になっているか確かめる（Claude に伝える）"))
    elif ok_cycle is None and started and now - started <= dt.timedelta(minutes=DECISION_STALE_MINUTES):
        findings.append(Finding("見張り役", "info", f"起動した（{started:%H:%M}）。最初の判断の回を待っている",
                                "なし（起動から数分で最初の回が回る）", "なし"))
    elif ok_cycle is None or now - ok_cycle > dt.timedelta(minutes=DECISION_STALE_MINUTES):
        findings.append(Finding("見張り役", "bad", "判断の回が実際の口座に届いていない",
                                "見張り役の判断が発注に届かない。" + covered,
                                "GMOコインの注文用のキーの IP 制限に 136.64.73.92 が入っているか確かめる。入っていれば Claude に伝える"))
    else:
        findings.append(Finding("見張り役", "ok", f"動いている（版 {watch.get('version') or '?'}・最後の回 {ok_cycle:%H:%M}）"))

    # 判断する者
    for name in accounts:
        h = decider_health(live_events, name, now, market_open=is_open)
        impact, action = "", ""
        if h["state"] == "bad" and h["text"].startswith("判断の回が止まっている"):
            impact = "新しく建てる・閉じる判断が出ない。建玉は GMOコイン側の損切り・利確で守られている"
            action = "見張り役とジョブの両方が止まっている。Claude に伝える"
        elif h["state"] == "bad":
            impact = ("AI が判断しないので、AI の口座は新しく建てない・閉じない（持っている建玉は損切り・利確で守られている）"
                      if name == "ai" else "このロジックの口座は判断できない（持っている建玉は損切り・利確で守られている）")
            action = "表示された理由を Claude に伝える（設定や権限の不具合のことが多い）"
        elif h["state"] == "warn":
            impact = "たまに失敗しているが、そのあと判断できている"
            action = "続くようなら Claude に伝える"
        findings.append(Finding(name, h["state"], h["text"], impact, action))

    # 口座
    if not live_enabled:
        findings.append(Finding("自動売買", "warn", "有効になっていない（fx_live_enabled）", "実際の売買をしない",
                                "売買させるなら infra/terraform.tfvars の fx_live_enabled = true で反映する"))
    elif killed:
        findings.append(Finding("自動売買", "bad", "キルスイッチが入っている", "新しく建てない（決済はする）",
                                "自分で入れたならそのまま。再開するなら「操作」の画面でキルスイッチを切る"))
    ready = next((e for e in reversed(live_events) if e.get("kind") == "live_ready"), None)
    if ready is not None and not ready.get("ok"):
        findings.append(Finding("口座", "bad", f"GMOコインの口座を照会できない（{str(ready.get('note', ''))[:80]}）",
                                "判断が発注に届かない。建玉は GMOコイン側の損切り・利確で守られている",
                                "ERR-5012 ならキーの権限か IP 制限。それ以外は Claude に伝える"))
    errors = [e for e in live_events if e.get("kind") in ("live_error", "error")
              and (t := _at(e.get("at"))) and now - t <= ERROR_WINDOW]
    if errors:
        last = errors[-1]
        text = str(last.get("error") or last.get("errors") or "")[:80]
        findings.append(Finding("口座", "warn", f"直近1時間に{len(errors)}回エラー（最後: {text}）",
                                "その回は照会・発注ができなかった。次の回でやり直す", "続くようなら Claude に伝える"))

    if calendar is not None and is_open:
        fetched = _at(calendar.get("fetched_at"))
        if fetched is None or now - fetched > CALENDAR_STALE:
            since = f"{fetched:%m-%d %H:%M} から" if fetched else "一度も"
            findings.append(Finding("経済指標の予定", "warn",
                                    f"{since}取れていない（{calendar.get('error') or 'まだ取りに行っていない'}）",
                                    "AI が発表の予定を知らずに判断する。短期売買（ペーパー）は発表の前後でも建てる。売買そのものは止まらない",
                                    "しばらく待っても直らなければ Claude に「経済指標の予定が取れていない」と伝える"))
        else:
            findings.append(Finding("経済指標の予定", "ok", f"取れている（{fetched:%H:%M} に取得）"))
    if paper_paused:
        findings.append(Finding("ペーパー運用", "info", "止めている", "実際の売買には関係ない",
                                "比べたいなら「操作」の画面で再開する"))

    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    return sorted(findings, key=lambda f: order.get(f.state, 9))


def worst(findings: Sequence[Finding]) -> str:
    """いちばん悪い状態。"""
    for state in ("bad", "warn", "info"):
        if any(f.state == state for f in findings):
            return state
    return "ok"
