"""AI（Claude）の4つの役割。拡張パーツ（本体からは import されない）。

  A 分析の相棒    検証の結果や警告を解説し、質問に答える（読むだけ）
  B 運用の見張り役 日次の状況を要約し、異常を指摘する（読むだけ）
  C アイデア出し  新しい売買ルールの案を、決まった形で出す（実行はしない）
  D 売買の判断    **ペーパー運用でのみ** 売買の候補を出す。リスク制限を必ず通す

## 守っていること

  * AI に発注の権限は無い。設定も変えられない。D の候補も、既存のリスク制限を通ったものだけ台帳に残る
  * AI に渡すのは、集計した数字と保有だけ。APIキーなどの秘密は渡さない
  * 費用には月の上限がある（``AiBudget``）。超えたら呼ばない
  * C の案は「試行」ではない。実装して検証にかけたときに初めて試行回数 N に数える。
    案を大量に出させて片端から試すと、偶然の当たりを拾う
  * D の判断は、モデルが学習した期間の過去データでは **先読み** になりうる（モデルはその頃の値動きを知っている）。
    評価に使えるのは、モデルの知識の期限より後の、新しいデータでの前向きの記録だけ
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

import numpy as np

from ...core.contracts import Order, Side
from ...core.gates import Mode
from .ports import LanguageModel, ModelReply, UsageLog

#: 使うモデル。料金は 100万トークンあたりの米ドル（2026年10月時点の公表値）
MODEL = "claude-opus-5-5"
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
}
#: フォールバック先など、表に無いモデルは高めに見積もる（費用の上限を甘くしないため）
FALLBACK_PRICE = {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25}

#: どの役割にも共通の前提。毎回同じ文面にして、プロンプトキャッシュが効くようにする
SYSTEM = """あなたは、個人が使う売買ルールの検証と少額運用のためのシステム「algo-trading-system」に組み込まれたアシスタントです。

このシステムの前提:
- 注文は出さない。人が証券会社・FX業者のアプリで発注し、約定を記録する
- 検証はランダム売買との比較、多重検定の補正（N回試した水準）、ロックボックス、ウォークフォワードで行う
- 1取引あたりの期待値と t 値を最も重く見る。勝率や総リターンだけで優位性を判断しない
- これまでの検証では、優位性があると言える売買ルールは見つかっていない

答え方:
- 日本語で、平易に、結論から書く
- 渡されたデータに無いことは推測で埋めない。分からないことは分からないと書く
- 投資の推奨はしない。数字の読み方と、確かめるべきことを示す
- 一見良い成績には、まずバグ・先読み・偶然・コスト前提を疑う"""

#: 役割ごとの考える深さ（effort）。説明と要約は medium、案と判断は high
EFFORT = {"explain": "medium", "report": "medium", "ideas": "high", "decide": "high"}


class AiError(RuntimeError):
    """AI を呼べない、または答えが使えない。"""


# ----------------------------------------------------------------------
# 答えの形（JSON Schema）
# ----------------------------------------------------------------------
REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["normal", "caution", "alert"]},
        "summary": {"type": "string"},
        "anomalies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "title", "detail"],
                "additionalProperties": False,
            },
        },
        "next_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "anomalies", "next_actions"],
    "additionalProperties": False,
}

IDEAS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "market": {"type": "string", "enum": ["jp_stock_daily", "fx_intraday"]},
                    "family": {
                        "type": "string",
                        "enum": ["momentum", "mean_reversion", "volatility", "breakout", "regime", "other"],
                    },
                    "rule": {"type": "string"},
                    "parameters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "values": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["name", "values"],
                            "additionalProperties": False,
                        },
                    },
                    "rationale": {"type": "string"},
                    "risks": {"type": "string"},
                    "test_plan": {"type": "string"},
                },
                "required": ["name", "market", "family", "rule", "parameters", "rationale", "risks", "test_plan"],
                "additionalProperties": False,
            },
        },
        "caution": {"type": "string"},
    },
    "required": ["ideas", "caution"],
    "additionalProperties": False,
}

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["hold", "buy", "sell"]},
        "symbol": {"type": "string"},
        "quantity": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reasoning": {"type": "string"},
        "risks": {"type": "string"},
    },
    "required": ["action", "symbol", "quantity", "confidence", "reasoning", "risks"],
    "additionalProperties": False,
}


# ----------------------------------------------------------------------
# 費用
# ----------------------------------------------------------------------
def cost_usd(usage: dict[str, int], model: str = MODEL) -> float:
    """1回の呼び出しの費用（米ドル）。キャッシュの読み書きも含める。"""
    per = PRICES.get(model, FALLBACK_PRICE)
    return (
        usage.get("input_tokens", 0) * per["input"]
        + usage.get("output_tokens", 0) * per["output"]
        + usage.get("cache_read_input_tokens", 0) * per["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * per["cache_write"]
    ) / 1_000_000


@dataclass(frozen=True)
class AiBudget:
    """月の費用の上限（米ドル）。上限に達したら呼ばない。"""

    monthly_usd: float = 5.0

    @staticmethod
    def month_start(today: dt.date) -> str:
        return today.replace(day=1).isoformat()


# ----------------------------------------------------------------------
# AI に渡す情報（集計だけ。秘密は含めない）
# ----------------------------------------------------------------------
def to_context(data: Any) -> str:
    """キーを並べ替えた JSON。毎回同じ並びにすると、比べやすくキャッシュにも優しい。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str, indent=1)


def status_context(status: dict[str, Any], stats: Any, curve: list, fills: list, audit: list) -> dict[str, Any]:
    """運用の状況を、AI に渡す形に集計する。"""
    equity = [e for _, e, _ in curve[-30:]]
    return {
        "mode": getattr(status["mode"], "name", str(status["mode"])),
        "strategy": status["strategy"],
        "params": status["params"],
        "equity": round(status["equity"]),
        "cash": round(status["cash"]),
        "positions": [
            {"symbol": p["symbol"], "quantity": p["quantity"], "value": round(p["value"])} for p in status["positions"]
        ],
        "equity_last_30_days": [round(e) for e in equity],
        "gates": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in status["gates"]],
        "stats": {
            "days": stats.days,
            "fills": stats.fills,
            "mean_slippage": stats.mean_slippage,
            "assumed_slippage": stats.assumed_slippage,
            "max_drawdown": stats.max_drawdown,
            "loss_breaches": stats.loss_breaches,
        },
        "recent_fills": [
            {k: f.get(k) for k in ("date", "symbol", "side", "quantity", "price", "ref_open", "source")}
            for f in fills[-10:]
        ],
        "recent_setting_changes": [
            {k: a.get(k) for k in ("at", "key", "old", "new", "reason")} for a in audit[:10]
        ],
        "planned_signals": len(status["planned"]),
    }


def research_context(records: list[dict[str, Any]], limit: int = 40) -> dict[str, Any]:
    """試行の記録（experiment_log）を、AI に渡す形に集計する。"""
    dev = [r for r in records if r.get("kind") == "dev"]
    latest: dict[str, dict] = {}
    for r in dev:  # 同じ候補は最新の1件だけ
        latest[r.get("label", "?")] = r
    rows = sorted(latest.values(), key=lambda r: -(r.get("sharpe_daily") or -9))[:limit]
    return {
        "trials_recorded": len(dev),
        "lockbox_evaluations": sum(r.get("kind") == "lockbox" for r in records),
        "candidates": [
            {
                "label": r.get("label"),
                "total_return": r.get("total_return"),
                "sharpe_annual": (r.get("sharpe_daily") or 0) * 245**0.5,
                "trades": r.get("trades"),
                "max_drawdown": r.get("max_drawdown"),
                "window": r.get("window"),
            }
            for r in rows
        ],
    }


def shortlist(view: Any, symbols: list[str], budget: float, size: int = 30) -> list[dict[str, Any]]:
    """D に渡す候補の銘柄。売買代金の大きい順に、1株が予算内のものだけ。

    全銘柄（4,000超）を渡すと読み切れず、費用もかさむので絞る。
    """
    frame = view.today()
    if frame.empty:
        return []
    price_col = "raw_close" if "raw_close" in frame.columns else "close"
    rows = []
    for symbol in symbols:
        closes = view.values(symbol, "close", 61)
        turnover = view.values(symbol, "turnover", 20) if "turnover" in frame.columns else np.array([])
        if len(closes) < 61 or symbol not in frame.index:
            continue
        price = float(frame.at[symbol, price_col])
        if not price or price * view.lot_size > budget:
            continue
        returns = np.diff(closes) / closes[:-1]
        rows.append({
            "symbol": symbol,
            "price": round(price, 1),
            "return_20d": round(float(closes[-1] / closes[-21] - 1), 4),
            "return_60d": round(float(closes[-1] / closes[0] - 1), 4),
            "volatility_20d": round(float(np.std(returns[-20:])), 4),
            "turnover_20d": float(np.median(turnover)) if len(turnover) else 0.0,
        })
    rows.sort(key=lambda r: -r["turnover_20d"])
    for r in rows:
        r["turnover_20d"] = round(r["turnover_20d"] / 1e8, 2)  # 億円
    return rows[:size]


# ----------------------------------------------------------------------
# 本体
# ----------------------------------------------------------------------
class AiAssistant:
    def __init__(self, model: LanguageModel, usage: UsageLog, budget: AiBudget = AiBudget()) -> None:
        self.model = model
        self.usage = usage
        self.budget = budget

    def spent_this_month(self, today: dt.date) -> float:
        return self.usage.spent_since(self.budget.month_start(today))

    def _ask(self, role: str, prompt: str, schema: dict | None, today: dt.date) -> ModelReply:
        spent = self.spent_this_month(today)
        if spent >= self.budget.monthly_usd:
            raise AiError(
                f"今月の AI の費用が上限（${self.budget.monthly_usd:.2f}）に達しています（${spent:.2f}）。"
                "上限は AI_MONTHLY_BUDGET_USD で変えられます"
            )
        reply = self.model.ask(system=SYSTEM, prompt=prompt, schema=schema, effort=EFFORT[role])
        self.usage.append({
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "role": role,
            "model": reply.model,
            "usage": reply.usage,
            "cost_usd": round(cost_usd(reply.usage, reply.model), 6),
        })
        if schema is not None and reply.data is None:
            raise AiError("AI の答えが決まった形になっていませんでした。もう一度試してください")
        return reply

    # A ---------------------------------------------------------------
    def explain(self, question: str, context: dict[str, Any], today: dt.date) -> ModelReply:
        if not question.strip():
            raise AiError("質問を入力してください")
        prompt = (
            "次のデータをもとに、質問に答えてください。\n\n"
            f"<data>\n{to_context(context)}\n</data>\n\n<question>\n{question.strip()}\n</question>"
        )
        return self._ask("explain", prompt, None, today)

    # B ---------------------------------------------------------------
    def daily_report(self, context: dict[str, Any], today: dt.date) -> ModelReply:
        prompt = (
            "運用の状況を要約し、異常があれば指摘してください。異常の例: 想定より大きいスリッページ、"
            "急な評価額の変化、増額の条件の後退、未約定の候補の溜まり、設定の緩め方の不自然さ、"
            "データの古さ。何も無ければ anomalies は空にしてください。\n\n"
            f"<status>\n{to_context(context)}\n</status>"
        )
        return self._ask("report", prompt, REPORT_SCHEMA, today)

    # C ---------------------------------------------------------------
    def propose(self, context: dict[str, Any], market: str, today: dt.date, count: int = 3) -> ModelReply:
        prompt = (
            f"これまでに試した候補と結果を踏まえて、まだ試していない売買ルールの案を最大{count}つ出してください。"
            f"対象は {market} です。\n"
            "- 日足（国内株）または分足（FX）の四本値と売買代金だけで計算できること\n"
            "- パラメータの候補は各2〜3個まで（試行回数 N が掛け算で増えるため）\n"
            "- 既に試した手法の焼き直しは避け、なぜ優位性がありうるかの理屈を書くこと\n"
            "- test_plan には、ランダム比較・多重検定の補正・ロックボックスでどう確かめるかを書くこと\n\n"
            f"<research>\n{to_context(context)}\n</research>"
        )
        return self._ask("ideas", prompt, IDEAS_SCHEMA, today)

    # D ---------------------------------------------------------------
    def decide(
        self, *, mode: Mode, as_of: dt.date, portfolio: dict[str, Any], candidates: list[dict[str, Any]], today: dt.date
    ) -> tuple[ModelReply, list[Order], str]:
        """ペーパー運用でのみ、売買の候補を1つ出す。返すのは (返答, 注文, 却下の理由)。

        注文はまだリスク制限を通っていない。呼び出し側で必ず通すこと。
        """
        if mode is not Mode.PAPER:
            raise AiError("AI の売買判断は、ペーパー運用（モード2）でのみ使えます")
        prompt = (
            f"{as_of} の引け時点の情報です。翌営業日の寄りに執行する売買を、最大1つだけ判断してください。\n"
            "- buy の symbol は candidates の中から、sell の symbol は保有の中から選ぶこと\n"
            "- quantity は株数（1株単位）。現金と1銘柄の予算の範囲で\n"
            "- 根拠が弱いときは hold を選ぶこと（hold のとき symbol は空文字、quantity は 0）\n"
            "- あなたの学習データに含まれる期間の値動きの知識を、判断に使わないこと\n\n"
            f"<portfolio>\n{to_context(portfolio)}\n</portfolio>\n\n<candidates>\n{to_context(candidates)}\n</candidates>"
        )
        reply = self._ask("decide", prompt, DECISION_SCHEMA, today)
        decision = reply.data or {}
        action = decision.get("action")
        symbol = (decision.get("symbol") or "").strip()
        quantity = int(decision.get("quantity") or 0)
        held = {p["symbol"]: p["quantity"] for p in portfolio.get("positions", [])}
        allowed = {c["symbol"] for c in candidates}

        if action == "hold":
            return reply, [], ""
        if quantity <= 0:
            return reply, [], "数量が0以下"
        if action == "buy" and symbol not in allowed:
            return reply, [], f"候補に無い銘柄（{symbol}）"
        if action == "sell" and symbol not in held:
            return reply, [], f"保有していない銘柄（{symbol}）"
        side = Side.BUY if action == "buy" else Side.SELL
        if side is Side.SELL:
            quantity = min(quantity, int(held[symbol]))
        reason = f"AI判断（確信度 {decision.get('confidence')}）: {decision.get('reasoning', '')[:80]}"
        return reply, [Order(symbol, side, float(quantity), reason=reason)], ""
