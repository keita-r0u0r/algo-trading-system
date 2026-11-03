"""画面に出す値を作る（純粋関数）。

GUI（Streamlit）は表示するだけにして、何をどう見せるかはここで決める。
ここは GUI なしでテストできる。**APIキーなどの秘密は、値ではなく「有無」だけを返す。**
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from typing import Any, Sequence

from ..core.gates import Check, IMPLEMENTED, Mode
from ..core.risk import RiskLimits, is_loosening

JST = dt.timezone(dt.timedelta(hours=9))

MODE_LABELS = {
    Mode.STOP: "0 停止",
    Mode.SIGNAL: "1 シグナルのみ",
    Mode.PAPER: "2 ペーパー",
    Mode.CONFIRMED: "3 確認付き発注",
    Mode.AUTO: "4 自動発注",
}

#: カード用の短い表記（幅が狭いと長い値は切れる）
MODE_SHORT = {Mode.STOP: "停止", Mode.SIGNAL: "シグナル", Mode.PAPER: "ペーパー", Mode.CONFIRMED: "確認付き", Mode.AUTO: "自動"}

LIMIT_LABELS = {
    "max_order_value": "1注文の上限額（円）",
    "max_position_share": "1銘柄の上限（評価額比）",
    "max_positions": "同時に持てる銘柄数",
    "max_daily_loss": "日次損失の上限（評価額比）",
}

#: 実運用で、これより古いデータからは候補を出さない（application と同じ値）
STALE_DAYS = 5


def secret_state(value: str | None) -> str:
    """秘密の値そのものは出さない。"""
    return "設定済み" if value else "未設定"


def freshness(latest: dt.date, today: dt.date) -> dict[str, Any]:
    age = (today - latest).days
    ok = age <= STALE_DAYS
    message = (
        f"最新 {latest}（{age}日前）"
        if ok
        else f"最新 {latest}（{age}日前）。古すぎるため、実運用の候補は出せません"
    )
    return {"age_days": age, "ok": ok, "message": message}


def mode_options() -> list[dict[str, Any]]:
    return [
        {"mode": mode, "label": label, "available": mode in IMPLEMENTED,
         "note": "" if mode in IMPLEMENTED else "未実装（証券会社・FX業者との連携が必要）"}
        for mode, label in MODE_LABELS.items()
    ]


def status_cards(status: dict[str, Any]) -> list[dict[str, str]]:
    held = sum(p["value"] for p in status["positions"])
    return [
        {"label": "実行モード", "value": MODE_SHORT[status["mode"]], "note": status["strategy"] or ""},
        {"label": "評価額", "value": f"{status['equity']:,.0f}円", "note": f"現金 {status['cash']:,.0f}円"},
        {"label": "保有", "value": f"{len(status['positions'])}銘柄", "note": f"{held:,.0f}円"},
        {"label": "増額の条件", "value": "満たしている" if status["gates_passed"] else "未達",
         "note": f"{sum(c.ok for c in status['gates'])} / {len(status['gates'])}"},
    ]


def gate_rows(checks: Sequence[Check]) -> list[dict[str, str]]:
    return [{"状態": "OK" if c.ok else "未", "条件": c.name, "内容": c.detail} for c in checks]


def position_rows(positions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"銘柄": p["symbol"], "数量": p["quantity"], "価格": round(p["price"], 2), "評価額": round(p["value"])}
        for p in positions
    ]


def signal_rows(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    side = {"buy": "買い", "sell": "売り"}
    return [
        {"#": s["id"], "判断日": s["as_of"], "売買": side.get(s["side"], s["side"]), "銘柄": s["symbol"],
         "数量": s["quantity"], "参考価格": s["ref_price"], "状態": s["status"], "理由": s["reason"]}
        for s in signals
    ]


def signal_choice(signal: dict[str, Any]) -> str:
    side = {"buy": "買い", "sell": "売り"}.get(signal["side"], signal["side"])
    return f"#{signal['id']} {signal['as_of']} {side} {signal['symbol']} {signal['quantity']:g}"


def fill_rows(fills: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for f in fills:
        slip = None
        if f.get("ref_open"):
            raw = f["price"] / f["ref_open"] - 1
            slip = round((raw if f["side"] == "buy" else -raw) * 100, 3)
        rows.append({"日付": f["date"], "売買": "買い" if f["side"] == "buy" else "売り", "銘柄": f["symbol"],
                     "数量": f["quantity"], "約定価格": f["price"], "手数料": f["fee"],
                     "始値とのずれ(%)": slip, "種類": f["source"]})
    return rows


def audit_rows(log: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"日時": r["at"], "項目": r["key"], "旧": r["old"], "新": r["new"], "理由": r["reason"]} for r in log]


def limit_change(old: RiskLimits, new: RiskLimits) -> dict[str, Any]:
    """変更の内容と、即時か24時間後か。"""
    before, after = asdict(old), asdict(new)
    rows = [
        {"項目": LIMIT_LABELS[k], "現在": before[k], "変更後": after[k],
         "向き": "緩める" if after[k] > before[k] else ("厳しくする" if after[k] < before[k] else "—")}
        for k in before
    ]
    loosening = is_loosening(old, new)
    changed = before != after
    if not changed:
        timing = "変更はありません"
    elif loosening:
        timing = "緩める変更を含むため、24時間後に有効になります（それまでは現在の制限のまま）"
    else:
        timing = "厳しくする変更なので、すぐ適用されます"
    return {"rows": rows, "changed": changed, "loosening": loosening, "timing": timing}


def fx_summary_cards(summary: dict[str, Any]) -> list[dict[str, str]]:
    def pct(v):
        return "—" if v is None else f"{v:+.1%}"

    return [
        {"label": "リターン", "value": pct(summary["total_return"]), "note": f"{summary['final']:,.0f}円"},
        {"label": "最大ドローダウン", "value": pct(summary["max_drawdown"]), "note": ""},
        {"label": "取引", "value": f"{summary['trades']}回",
         "note": "" if summary["win_rate"] is None else f"勝率 {summary['win_rate']:.0%}"},
        {"label": "1取引の期待値", "value": "—" if summary["expectancy"] is None else f"{summary['expectancy']:+.2f}円",
         "note": "" if summary["t_stat"] is None else f"t値 {summary['t_stat']:+.2f}"},
        {"label": "ロスカット", "value": "あり" if summary["losscut"] else "なし",
         "note": "見送り " + str(summary.get("skipped_entries", 0)) + "回"},
    ]


#: FX のペーパー運用の口座の、画面に出す名前
ACCOUNT_LABELS = {
    "ai": "AI",
    "harness": "ハーネス＋決まり",
    "jev": "Jev",
    "rule_hourly_breakout": "1時間足の突破",
    "buy_and_hold": "買って持つだけ",
    "no_trade": "一切売買しない",
    "scalp_fade": "短期・逆張り",
    "scalp_follow": "短期・順張り",
}
PAIR_LABELS = {"USD_JPY": "ドル円", "EUR_JPY": "ユーロ円", "GBP_JPY": "ポンド円", "AUD_JPY": "豪ドル円"}


def account_label(name: str) -> str:
    """口座の内部の名前を、画面に出す名前にする（知らない名前はそのまま）。"""
    if name in ACCOUNT_LABELS:
        return ACCOUNT_LABELS[name]
    if name.startswith("carry_trend_"):
        pair = name.removeprefix("carry_trend_")
        return f"金利差＋流れ（{PAIR_LABELS.get(pair, pair)}）"
    return name


def account_kind(name: str) -> str:
    if name == "ai":
        return "AI"
    if name == "harness":
        return "機械（AI・Jev は見送りだけ）"
    if name == "jev":
        return "Jev（損切り幅は機械）"
    return "基準" if name in ("buy_and_hold", "no_trade") else "機械"


# 判断する者の動き具合は、健康診断の部品（application/fx_health.py）にある
from ..application.fx_health import decider_health  # noqa: E402,F401


# --- 判断を人が読める文にする ---------------------------------------------------------
def holdings_over_time(events: Sequence[dict[str, Any]]) -> list[bool | None]:
    """記録の各行の時点で、その行の判断する者が実際の建玉を持っていたか（判断の行以外は None）。"""
    held: set[str] = set()
    result: list[bool | None] = []
    for e in events:
        kind, account = e.get("kind"), e.get("account")
        result.append(account in held if kind == "live_decision" else None)
        if kind == "live_open" and e.get("position_id"):
            held.add(account)
        elif kind in ("live_close", "live_gone") and account:
            held.discard(account)
        elif kind == "live_fatal":
            held.clear()
    return result


def action_label(decision: dict[str, Any], holding: bool | None) -> str:
    """判断の名前。「見送り」は、持っているなら「持ち続ける」、持っていないなら「建てない」と分ける。"""
    action = decision.get("action")
    if action == "hold":
        return {True: "持ち続ける", False: "建てない（様子見）"}.get(holding, "見送り")
    pair = PAIR_LABELS.get(decision.get("symbol", ""), decision.get("symbol", ""))
    return {"open_long": f"{pair}を買う", "open_short": f"{pair}を売る", "close": "閉じる"}.get(action, action or "—")


def _yen(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "—"


def explain_decision(e: dict[str, Any], holding: bool | None = None) -> str:
    """判断の理由を、ふつうの文で。AI はその根拠、ロジックは見た数字と決まりの当てはめ。"""
    decision = e.get("decision") or {}
    if e.get("error"):
        return f"AI を呼べなかったので、何もしなかった（{str(e['error'])[:80]}）"
    if e.get("account") == "harness":
        return harness_reason(e)
    if e.get("note"):
        note = str(e["note"])
        if note.startswith("AI を呼ぶ回ではない（"):  # 理由を記録する前の版
            return "AI を呼ぶ回ではないので、何もしなかった（AI は15分ごとと、見張り役が急変などで起こしたときに呼ぶ）"
        return note
    if decision.get("reasoning"):
        return str(decision["reasoning"])
    logic = e.get("logic") or {}
    account = str(e.get("account", ""))
    if account.startswith("carry_trend"):
        carry, last, average = logic.get("carry_percent"), logic.get("last_close"), logic.get("average_50d")
        parts = []
        if carry is None:
            parts.append("金利差がわからない")
        else:
            parts.append(f"金利差は {carry:+.2f}%（{'プラスなので買う側が有利' if carry > 0 else 'プラスではない'}）")
        if last is None or average is None:
            parts.append("50営業日の平均が出せない")
        else:
            parts.append(f"前の営業日の終値 {_yen(last)} は50営業日の平均 {_yen(average)} より"
                         f"{'上（上向きの流れ）' if last > average else '下（流れが弱い）'}")
        met = logic.get("target") == 1
        conclusion = ("2つの条件がそろっている" if met else "条件がそろっていない")
        if holding or logic.get("held"):
            tail = "ので、持ち続ける" if met else "ので、閉じる"
        else:
            tail = "ので、買う" if met else "ので、建てない"
        sizing = ""
        if met and logic.get("risk_fraction") and not (holding or logic.get("held")):
            sizing = f"。流れの強さ {logic.get('trend_scale')} から、1回の損切りで資金の {float(logic['risk_fraction']):.1%} まで"
        return "。".join(parts) + "。" + conclusion + tail + sizing
    if account == "rule_hourly_breakout":
        last, high, low = logic.get("last_close"), logic.get("high_20h"), logic.get("low_20h")
        if last is None or high is None or low is None:
            return "1時間足がまだ足りない"
        where = ("直近20本の高値を上抜けた" if last > high else "直近20本の安値を下抜けた" if last < low
                 else f"直近20本の高値 {_yen(high)} と安値 {_yen(low)} の間にある")
        return f"1時間足の終値 {_yen(last)} は{where}" + ("ので、その向きに建てる" if decision.get("action") != "hold" else "ので、何もしない")
    if account == "buy_and_hold":
        return "持っているので、そのまま" if logic.get("held") else "持っていないので、買う"
    if account == "no_trade":
        return "一切売買しない（比べる基準）"
    return ""


def harness_reason(e: dict[str, Any]) -> str:
    """ハーネス＋決まりの判断を、見た数字つきの文で（建てた・閉じた理由、持っている間の線、建てなかった理由）。"""
    decision, logic = e.get("decision") or {}, e.get("logic") or {}
    note = f"（{e['note']}）" if e.get("note") else ""
    if decision.get("reasoning"):
        return str(decision["reasoning"]) + note
    evidence = logic.get("evidence")
    if isinstance(evidence, list):  # 持っている間
        return "持ち続ける: " + " / ".join(evidence)
    if isinstance(evidence, dict):  # 建てる候補を探した
        parts = [f"{PAIR_LABELS.get(sym, sym)}: {why[-1]}" for sym, why in evidence.items() if why]
        return ("建てない" + note + "。" if note else "建てない。") + " / ".join(parts)
    return str(e.get("note") or "")


def decider_scorecard(events: Sequence[dict[str, Any]], account: str) -> dict[str, Any]:
    """判断する者の実際の成績。勝率より、1回の勝ちの大きさと、乗った含み益をどれだけ残せたかを先に見る。"""
    opens = {int(e["position_id"]): e for e in events
             if e.get("kind") == "live_open" and e.get("account") == account and e.get("position_id")}
    pnl: dict[int, float] = {}
    best: dict[int, float] = {}
    for e in events:
        if e.get("kind") == "execution" and e.get("settleType") == "CLOSE" and int(e.get("positionId") or 0) in opens:
            pid = int(e["positionId"])
            pnl[pid] = pnl.get(pid, 0.0) + float(e.get("lossGain") or 0)
        elif e.get("kind") == "snapshot":
            for p in e.get("positions") or []:
                pid = int(p.get("positionId") or 0)
                if pid in opens:
                    best[pid] = max(best.get(pid, 0.0), float(p.get("lossGain") or 0))
    results = [pnl[pid] for pid in opens if pid in pnl]
    wins, losses = [x for x in results if x > 0], [x for x in results if x <= 0]
    peaked = [pid for pid in opens if pid in pnl and best.get(pid, 0) >= 20]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    return {"trades": len(results), "total": round(sum(results), 1),
            "win_rate": len(wins) / len(results) if results else None,
            "avg_win": round(avg_win, 1) if avg_win is not None else None,
            "avg_loss": round(avg_loss, 1) if avg_loss is not None else None,
            "payoff": round(avg_win / -avg_loss, 2) if avg_win and avg_loss else None,
            "peaked": len(peaked),
            "kept_of_peak": (round(sum(pnl[p] for p in peaked) / sum(best[p] for p in peaked), 2) if peaked else None)}


def filter_shadow_rows(events: Sequence[dict[str, Any]], account: str = "harness") -> list[dict[str, Any]]:
    """決まりが建てた取引ごとの、Jev / AI の見送りの答えと、その取引の結果（記録だけの答えを後で比べる）。"""
    opens = [e for e in events if e.get("kind") == "live_open" and e.get("account") == account]
    pnl: dict[int, float] = {}
    for e in events:
        if e.get("kind") == "execution" and e.get("settleType") == "CLOSE" and e.get("positionId") is not None:
            pnl[int(e["positionId"])] = pnl.get(int(e["positionId"]), 0.0) + float(e.get("lossGain") or 0)
    decisions = [e for e in events if e.get("kind") == "live_decision" and e.get("account") == account
                 and str((e.get("decision") or {}).get("action", "")).startswith("open") and e.get("approved")]
    rows = []
    for o in opens:
        d = next((x for x in reversed(decisions) if x.get("at", "") <= o.get("at", "")
                  and (x.get("decision") or {}).get("symbol") == o.get("symbol")), None)
        f = ((d or {}).get("logic") or {}).get("filter") or {}
        rows.append({"at": o.get("at", ""), "symbol": o.get("symbol"), "side": o.get("side"),
                     "provider": f.get("provider"), "skip_probability": f.get("skip_probability"),
                     "said_skip": f.get("veto"), "error": f.get("error"),
                     "pnl": round(pnl[int(o["position_id"])], 1) if int(o.get("position_id") or 0) in pnl else None})
    return rows


def parse_changelog(text: str) -> dict[int, dict[str, str]]:
    """CHANGELOG.md を PR 番号ごとの {date, title, body} にする（見出しは「## #52 · 2026-11-04 · 題」）。"""
    entries: dict[int, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if line.startswith("## #"):
            parts = [p.strip() for p in line[3:].split("·")]
            try:
                number = int(parts[0].lstrip("#"))
            except ValueError:
                current = None
                continue
            current = {"date": parts[1] if len(parts) > 1 else "", "title": parts[-1], "body": ""}
            entries[number] = current
        elif current is not None:
            current["body"] += line + "\n"
    for e in entries.values():
        e["body"] = e["body"].strip()
    return entries


def release_rows(releases: Sequence[dict[str, Any]], changelog: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    """反映の記録（state/releases.jsonl）を新しい順に、入った PR の題と中身つきで。"""
    rows = []
    for r in reversed(releases):
        prs = [int(n) for n in r.get("prs") or []]
        rows.append({"version": r.get("version"), "at": str(r.get("verified_at") or r.get("at") or ""),
                     "result": r.get("result"), "previous": r.get("previous"), "note": r.get("note", ""),
                     "changes": [{"pr": n, "title": changelog.get(n, {}).get("title") or (r.get("titles") or {}).get(str(n), ""),
                                  "body": changelog.get(n, {}).get("body", "")} for n in prs]})
    return rows


def ai_last_real_call(events: Sequence[dict[str, Any]], account: str = "ai") -> dt.datetime | None:
    """AI を実際に呼んだ最後の時刻（呼ばなかった回・失敗した回は除く）。"""
    for e in reversed(events):
        if (e.get("kind") == "live_decision" and e.get("account") == account and e.get("model")
                and not e.get("note") and not e.get("error") and not e.get("reused")):
            return dt.datetime.fromisoformat(e.get("logged_at") or e["at"])
    return None


def ai_next_looks(row: dict[str, Any], price: float | None, now: dt.datetime, ai_last: dt.datetime | None,
                  rules: dict[str, Any], next_session: str | None, *, interval_minutes: int = 15,
                  pip: float = 0.01) -> list[str]:
    """この建玉を、次に AI が見るきっかけ（見張り役の線と同じ）。早い・近い順ではなく、きっかけの種類ごとに並べる。"""
    looks = []
    due = (ai_last + dt.timedelta(minutes=interval_minutes)) if ai_last else now
    looks.append(f"定期: {max(due, now).astimezone(JST):%H:%M} ごろ（15分ごと）")
    near = float(rules.get("near_pips", 3))
    for label, level in (("損切り", row.get("stop")), ("利確", row.get("take"))):
        if level is None or price is None:
            continue
        line = level + near * pip if price > level else level - near * pip
        looks.append(f"{label}の手前: {line:.3f} に{'下がった' if price > level else '上がった'}ら（{label} {level:.3f} の {near:g} pips 手前）")
    looks.append(f"急変: {rules.get('window_minutes', 5)}分で ±{rules.get('move_pips', 15):g} pips 動いたら")
    if next_session:
        looks.append(f"時間帯の切り替わり: {next_session}")
    return looks


def _business_days(start: dt.datetime, end: dt.datetime) -> float:
    """start から end までの平日の日数（端は時刻の割合で数える）。"""
    days, t = 0.0, start
    while t < end:
        nxt = min(end, (t + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        if t.weekday() < 5:
            days += (nxt - t).total_seconds() / 86400
        t = nxt
    return days


def month_outlook(points: Sequence[tuple[dt.datetime, float]], now: dt.datetime, *, target: float = 1.5) -> dict[str, Any]:
    """今月の目標（月初の評価額 × target）に対する、今のペースでの月末の見込みと、届く確からしさ。

    見込みは「月初から今までの平日1日あたりの伸び」を残りの平日に伸ばしたもの。確からしさは、日ごとの伸びの
    ばらつきから「月末に目標以上になる割合」を見積もる（平日5日分より少ないと、出さない）。
    """
    import math

    month = [(t, v) for t, v in points if v > 0 and (t.year, t.month) == (now.year, now.month)]
    if not month:
        return {}
    start_at, start = month[0]
    current = month[-1][1]
    end = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elapsed, remaining = _business_days(start_at, now), _business_days(now, end)
    goal = start * target
    out = {"start": start, "start_at": start_at, "current": current, "goal": goal,
           "elapsed_days": round(elapsed, 1), "remaining_days": round(remaining, 1),
           "required_daily": (goal / current) ** (1 / remaining) - 1 if remaining > 0 and current > 0 else None}
    pace = (current / start) ** (1 / elapsed) if elapsed >= 0.5 else None
    out["daily_pace"] = pace - 1 if pace else None
    out["projected"] = current * pace ** remaining if pace else None
    closes: dict[dt.date, float] = {}
    for t, v in month:
        if t.weekday() < 5:
            closes[t.date()] = v
    values = [start] + list(closes.values())
    rets = [math.log(b / a) for a, b in zip(values, values[1:]) if a > 0 and b > 0]
    out["days_of_data"] = len(rets)
    if len(rets) >= 5 and remaining > 0:
        mu = sum(rets) / len(rets)
        sd = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        if sd > 0:
            z = (math.log(goal / current) - mu * remaining) / (sd * math.sqrt(remaining))
            out["chance"] = 0.5 * math.erfc(z / math.sqrt(2))
    return out


def monthly_ai_cost(records: Sequence[dict[str, Any]], now: dt.datetime) -> dict[str, float]:
    """今月の AI の費用（実績）と、今のペースでの月末の見込み（日本時間の月で数える）。"""
    spent = sum(float(r.get("cost_usd") or 0) for r in records
                if r.get("kind", "call") == "call" and r.get("at")
                and (t := dt.datetime.fromisoformat(r["at"]).astimezone(JST)).year == now.year and t.month == now.month)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elapsed, total = _business_days(start, now), _business_days(start, end)
    return {"spent": spent, "projected": spent / elapsed * total if elapsed > 0.2 else spent}
