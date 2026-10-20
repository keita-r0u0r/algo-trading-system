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
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from ...core.contracts import Order, Side
from ...core.gates import Mode
from .models import DEFAULT_MODEL, ModelSettings, cost_usd, split
from .ports import LanguageModel, ModelReply, UsageLog

#: 既定のモデル（互換のため残す。実際に使うモデルは ModelSettings で決まる）
MODEL = DEFAULT_MODEL

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
EFFORT = {"explain": "medium", "report": "medium", "ideas": "high", "decide": "high", "fx": "medium"}


class AiError(RuntimeError):
    """AI を呼べない、または答えが使えない。"""


class RefusalError(AiError):
    """安全上の理由で、モデルが答えを控えた。切り替えの順番があれば、次のモデルで試す。"""


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

#: E: FX のペーパー運用の判断。決まった枠の中で、通貨ペア・向き・リスク・損切り・利確を自由に決める
FX_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["hold", "open_long", "open_short", "close"]},
        "symbol": {"type": "string"},
        "risk_fraction": {"type": "number"},
        "stop_pips": {"type": "number"},
        "take_pips": {"type": "number"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "horizon": {"type": "string"},
        "reasoning": {"type": "string"},
        "risks": {"type": "string"},
    },
    "required": ["action", "symbol", "risk_fraction", "stop_pips", "take_pips", "confidence", "horizon",
                 "reasoning", "risks"],
    "additionalProperties": False,
}


# ----------------------------------------------------------------------
# 費用
# ----------------------------------------------------------------------
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
    def __init__(
        self,
        model: LanguageModel,
        usage: UsageLog,
        budget: AiBudget = AiBudget(),
        settings: ModelSettings | None = None,
    ) -> None:
        self.model = model
        self.usage = usage
        self.budget = budget
        self.settings = settings or ModelSettings()

    def model_for(self, role: str) -> str:
        return self.settings.load().model_for(role)

    def spent_this_month(self, today: dt.date) -> float:
        return self.usage.spent_since(self.budget.month_start(today))

    def _ask(self, role: str, prompt: str, schema: dict | None, today: dt.date) -> ModelReply:
        spent = self.spent_this_month(today)
        if spent >= self.budget.monthly_usd:
            raise AiError(
                f"今月の AI の費用が上限（${self.budget.monthly_usd:.2f}）に達しています（${spent:.2f}）。"
                "上限は AI_MONTHLY_BUDGET_USD で変えられます"
            )
        attempts = self.settings.load().attempts(role)
        refused: list[str] = []
        for key in attempts:
            reply = self._attempt(role, key, prompt, schema)
            if isinstance(reply, RefusalError):
                refused.append(key)  # 断られた記録は _attempt が残している。次のモデルで試す
                continue
            return reply
        raise RefusalError(
            "AI が答えを控えました。試したモデル: " + " → ".join(refused) + "。質問の書き方を変えて試してください"
        )

    def _attempt(self, role: str, key: str, prompt: str, schema: dict | None) -> ModelReply | RefusalError:
        """1つのモデルで試す。断られたら RefusalError を返し（投げない）、次のモデルに回す。"""
        call_id = uuid.uuid4().hex[:12]
        record: dict[str, Any] = {
            "kind": "call",
            "call_id": call_id,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "role": role,
            "model": key,
        }
        started = time.monotonic()
        try:
            reply = self.model.ask(model=key, system=SYSTEM, prompt=prompt, schema=schema, effort=EFFORT[role])
        except RefusalError as exc:
            self.usage.append({**record, "ok": False, "error": str(exc)[:200],
                               "latency_ms": int((time.monotonic() - started) * 1000), "cost_usd": 0.0})
            return exc
        except AiError as exc:
            # 失敗も記録する（モデルごとの失敗率の集計に使う）。費用は分からないので0
            self.usage.append({**record, "ok": False, "error": str(exc)[:200],
                               "latency_ms": int((time.monotonic() - started) * 1000), "cost_usd": 0.0})
            raise
        latency = int((time.monotonic() - started) * 1000)
        schema_ok = schema is None or reply.data is not None
        provider, model_id = split(key)
        served = reply.served_model or model_id
        # サーバー側の切り替えが起きたら、実際に答えたモデルの料金で数える
        price_key = key if served == model_id else f"{provider}:{served}"
        self.usage.append({
            **record,
            "served_model": served,
            "ok": schema_ok,
            "error": "" if schema_ok else "決まった形で返らなかった",
            "latency_ms": latency,
            "usage": reply.usage,
            "cost_usd": round(cost_usd(reply.usage, price_key), 6),
            # D の判断は、あとで戦績と結び付けるために中身も残す
            "decision": reply.data if role in ("decide", "fx") else None,
        })
        if not schema_ok:
            raise AiError("AI の答えが決まった形になっていませんでした。もう一度試してください")
        return replace(reply, model=key, call_id=call_id, latency_ms=latency)

    def rate(self, call_id: str, score: int, note: str = "") -> None:
        """答えの良し悪しを記録する（1〜5）。モデルごとの評価の集計に使う。"""
        if not call_id or score not in (1, 2, 3, 4, 5):
            raise AiError("評価は1〜5で付けてください")
        model = next((r.get("model") for r in self.usage.records() if r.get("call_id") == call_id), None)
        if model is None:
            raise AiError("評価する呼び出しが見つかりません")
        self.usage.append({
            "kind": "rating",
            "call_id": call_id,
            "model": model,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "score": score,
            "note": note[:200],
        })

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

    # E ---------------------------------------------------------------
    def fx_decide(self, context: dict[str, Any], today: dt.date) -> ModelReply:
        """FX のペーパー運用で、次の1時間の判断を1つ出す。枠の確認は呼び出し側（core.fx.frame）で行う。"""
        prompt = (
            "FX のペーパー運用（仮想の約定。実際の注文は出ない）で、今の時点の判断を1つだけ出してください。\n"
            "- 守る枠は <frame> のとおり。枠の外の判断は実行されず、記録だけ残る\n"
            "- open_long / open_short は新規。symbol は frame.symbols から。stop_pips は必須で範囲内\n"
            "- risk_fraction は「損切りに掛かったとき資金の何割を失ってよいか」（例 0.01 = 1%）。上限は frame.max_risk_per_trade\n"
            "- take_pips は利確の幅。付けないなら 0\n"
            "- 建玉を持っている間は、close か hold だけ。向きを変えたいなら、まず close\n"
            "- コストは往復で約1 pip（スプレッド・手数料・ずれ）。それより十分大きい値幅を見込めないなら hold\n"
            "- 根拠が弱いときは hold を選ぶ。hold のとき symbol は空文字、数値は 0\n"
            "- あなたの学習データに含まれる期間の値動きの知識を、判断に使わないこと。渡したデータだけで判断する\n\n"
            f"<frame>\n{to_context(context.get('frame', {}))}\n</frame>\n\n"
            f"<account>\n{to_context(context.get('account', {}))}\n</account>\n\n"
            f"<market>\n{to_context(context.get('market', {}))}\n</market>"
        )
        return self._ask("fx", prompt, FX_DECISION_SCHEMA, today)

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
