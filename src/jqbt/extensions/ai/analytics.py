"""モデルごとの戦績と特徴（純粋関数）。

## 集めるもの

  * 特徴: 呼び出し回数、失敗率、断られた回数、待ち時間、費用、利用者の評価（1〜5）
  * D の戦績: 判断の内訳（買い・売り・見送り）と確信度、そのモデルの判断で **買った株** を売ったときの損益

損益の結び付け: D の候補は理由の先頭に ``[AI:<モデル>:<呼び出しID>]`` が付いて台帳に残る。
銘柄ごとに、先に買ったものから売りに充てる（先入れ先出し）。売りがどこから来たか（AI・戦略・手入力）は問わない。

## 読むときの注意（``caveats`` が返す）

  * 取引数が少ないと、勝率も損益も運の範囲を出ない（目安30件）
  * モデルを比べること自体が「何通りも試す」ことになる。一番良く見えるモデルには偶然のゲタが乗る
  * モデルごとに学習した期間が違う。新しいモデルほど最近の値動きを知っていて、過去データでは先読みが大きい
  * ペーパーの約定は想定のずれをそのまま入れている。実際の約定のずれは含まない
"""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from typing import Any

from .models import PROVIDERS, normalize, spec, split

#: [AI:<キー>:<呼び出しID>]。キーは「呼び出し先:モデルID」なのでコロンを含む
TAG = re.compile(r"^\[AI:(.+):([0-9a-f]{6,})\]")
MIN_TRADES = 30


def tag_of(reason: str | None) -> tuple[str, str] | None:
    """売買候補の理由から、(モデル, 呼び出しID) を取り出す。AI 以外の候補は None。"""
    match = TAG.match(reason or "")
    return (match.group(1), match.group(2)) if match else None


def trade_outcomes(signals: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """モデルごとの、閉じた取引の損益（円）と、まだ持っている数量。"""
    model_of_signal = {s["id"]: tag_of(s.get("reason"))[0] for s in signals if tag_of(s.get("reason"))}
    lots: dict[str, deque] = defaultdict(deque)  # 銘柄 → [数量, 単価(手数料込み), モデル]
    result: dict[str, dict[str, Any]] = defaultdict(lambda: {"pnls": [], "open_quantity": 0.0})

    for fill in sorted(fills, key=lambda f: (f["date"], f["id"])):
        quantity, price, fee = float(fill["quantity"]), float(fill["price"]), float(fill.get("fee") or 0.0)
        if fill["side"] == "buy":
            model = model_of_signal.get(fill.get("signal_id"))
            lots[fill["symbol"]].append([quantity, price + fee / quantity, model])
            continue
        # 売り: 先に買ったものから充てる。モデルごとに、この売りでの損益を1件の取引として数える
        remaining, per_model = quantity, defaultdict(float)
        net = price - fee / quantity
        queue = lots[fill["symbol"]]
        while remaining > 1e-9 and queue:
            lot = queue[0]
            used = min(remaining, lot[0])
            if lot[2]:
                per_model[lot[2]] += (net - lot[1]) * used
            lot[0] -= used
            remaining -= used
            if lot[0] <= 1e-9:
                queue.popleft()
        for model, pnl in per_model.items():
            result[model]["pnls"].append(pnl)

    for queue in lots.values():
        for quantity, _, model in queue:
            if model:
                result[model]["open_quantity"] += quantity
    return dict(result)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _t_stat(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean / math.sqrt(var / n) if var > 0 else None


def model_report(
    records: list[dict[str, Any]], signals: list[dict[str, Any]], fills: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """モデルごとの集計を、費用の多い順に返す。"""
    calls = [r for r in records if r.get("kind", "call") == "call"]
    ratings = [r for r in records if r.get("kind") == "rating"]
    outcomes = trade_outcomes(signals, fills)
    # 古い記録（呼び出し先の無いモデル名）も、今の書き方にそろえて集計する
    for r in calls + ratings:
        if r.get("model"):
            r["model"] = normalize(r["model"])
    outcomes = {normalize(k): v for k, v in outcomes.items()}
    models = sorted({r.get("model") for r in calls if r.get("model")} | set(outcomes))

    rows = []
    for model in models:
        mine = [r for r in calls if r.get("model") == model]
        ok = [r for r in mine if r.get("ok")]
        refused = [r for r in mine if "控えました" in (r.get("error") or "")]
        decisions = [r.get("decision") or {} for r in ok if r.get("role") == "decide"]
        scores = [r["score"] for r in ratings if r.get("model") == model]
        pnls = outcomes.get(model, {}).get("pnls", [])
        roles: dict[str, int] = defaultdict(int)
        for r in mine:
            roles[r.get("role", "?")] += 1
        rows.append({
            "model": model,
            "label": f"{spec(model).label}（{PROVIDERS.get(split(model)[0], split(model)[0])}）",
            "calls": len(mine),
            "roles": dict(roles),
            "success_rate": len(ok) / len(mine) if mine else None,
            "refusals": len(refused),
            "mean_latency_s": _mean([r.get("latency_ms", 0) / 1000 for r in ok]),
            "cost_usd": sum(r.get("cost_usd", 0.0) for r in mine),
            "cost_per_call_usd": _mean([r.get("cost_usd", 0.0) for r in ok]),
            "rating": _mean(scores),
            "ratings": len(scores),
            "decisions": len(decisions),
            "actions": {a: sum(d.get("action") == a for d in decisions) for a in ("buy", "sell", "hold")},
            "confidence": {c: sum(d.get("confidence") == c for d in decisions) for c in ("low", "medium", "high")},
            "trades": len(pnls),
            "pnl_total": sum(pnls) if pnls else 0.0,
            "pnl_mean": _mean(pnls),
            "win_rate": sum(p > 0 for p in pnls) / len(pnls) if pnls else None,
            "t_stat": _t_stat(pnls),
            "open_quantity": outcomes.get(model, {}).get("open_quantity", 0.0),
        })
    rows.sort(key=lambda r: -r["cost_usd"])
    return rows


def caveats(rows: list[dict[str, Any]]) -> list[str]:
    """集計を読むときの注意。"""
    notes = []
    traded = [r for r in rows if r["trades"]]
    if any(r["trades"] < MIN_TRADES for r in traded):
        notes.append(f"取引が{MIN_TRADES}件未満のモデルがあります。勝率も損益も、この件数では運の範囲を出ません")
    if any(r["t_stat"] is not None and abs(r["t_stat"]) < 2 for r in traded):
        notes.append("t値の絶対値が2未満のモデルがあります。優位性があるとも、劣っているとも言えません")
    if len(traded) >= 2:
        notes.append(
            f"{len(traded)}つのモデルを比べています。比べる数が多いほど、一番良く見えるモデルには偶然のゲタが乗ります"
        )
    if any(r["decisions"] for r in rows):
        notes.append(
            "モデルごとに学習した期間が違います。新しいモデルほど最近の値動きを知っていて、過去データでの判断は先読みが大きくなります"
        )
        notes.append("ペーパーの約定は、想定のずれをそのまま入れています。実際の約定のずれは含みません")
    if any(r["ratings"] and r["ratings"] < 10 for r in rows):
        notes.append("評価が10件未満のモデルがあります。評価の平均は、まだ傾向を見る程度にしてください")
    return notes
