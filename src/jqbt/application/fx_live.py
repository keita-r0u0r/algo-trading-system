"""FX の実際の約定（GMOコイン）を記録し、ペーパー運用と比べるための計算（I/O なし）。

実際の約定は人がアプリで発注したもの。このシステムは **読むだけ**（注文は出さない）。

  * 同じ約定を二重に数えない（約定IDで重複を除く）
  * 約定の値段と、その1分足の始まりの買値・売値の差（ずれ、pips）。正は不利な側
  * 手数料・確定した損益・スワップの合計
"""

from __future__ import annotations

from typing import Any, Iterable


def new_executions(fetched: Iterable[dict[str, Any]], known_ids: set[str]) -> list[dict[str, Any]]:
    """まだ記録していない約定だけ（古い順）。"""
    fresh = [e for e in fetched if str(e.get("executionId")) not in known_ids]
    return sorted(fresh, key=lambda e: (e.get("timestamp", ""), str(e.get("executionId"))))


def slippage_pips(side: str, price: float, bid: float, ask: float, pip: float = 0.01) -> float:
    """約定の値段が、見えていた値段よりどれだけ不利だったか（pips）。買いは買値、売りは売値と比べる。"""
    if side == "BUY":
        return round((price - ask) / pip, 2)
    return round((bid - price) / pip, 2)


def summarize(executions: list[dict[str, Any]]) -> dict[str, Any]:
    """約定の集計。数値は文字列で届くので、ここで数にする。"""
    def num(e: dict, key: str) -> float:
        try:
            return float(e.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    slips = [e["slippage_pips"] for e in executions if e.get("slippage_pips") is not None]
    closes = [e for e in executions if e.get("settleType") == "CLOSE"]
    return {
        "executions": len(executions),
        "closed": len(closes),
        "loss_gain": round(sum(num(e, "lossGain") for e in closes), 1),
        "fee": round(sum(num(e, "fee") for e in executions), 2),
        "swap": round(sum(num(e, "settledSwap") for e in closes), 1),
        "slippage_avg": round(sum(slips) / len(slips), 2) if slips else None,
        "slippage_worst": max(slips) if slips else None,
    }
