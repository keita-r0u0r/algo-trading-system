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
