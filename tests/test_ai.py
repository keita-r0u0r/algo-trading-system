"""AI パーツ。本物の API は呼ばず、偽のモデル・偽のクライアントで約束を固定する。

  * 費用の上限を超えたら呼ばない。呼ぶたびに費用を記録する
  * 決まった形で返らなければ使わない
  * D（売買判断）はペーパー運用でのみ。候補に無い銘柄・持っていない銘柄は使わない。
    使える判断も、運用サービスのリスク制限を必ず通る
  * Claude の呼び出しは、effort・構造化出力・フォールバックを付け、断り（refusal）と切れ（max_tokens）を扱う
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from jqbt.core.gates import Mode
from jqbt.extensions.ai.assistant import (
    DECISION_SCHEMA,
    EFFORT,
    REPORT_SCHEMA,
    AiAssistant,
    AiBudget,
    AiError,
    cost_usd,
    research_context,
)
from jqbt.extensions.ai.claude import FALLBACK_BETA, ClaudeModel
from jqbt.extensions.ai.ports import ModelReply
from jqbt.extensions.ai.usage import JsonlUsageLog

TODAY = dt.date(2026, 10, 18)
USAGE = {"input_tokens": 1_000, "output_tokens": 500, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


class FakeModel:
    def __init__(self, data=None, text="答え"):
        self.data, self.text, self.calls = data, text, []

    def ask(self, *, system, prompt, schema=None, effort="medium"):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema, "effort": effort})
        return ModelReply(text=self.text, data=self.data if schema else None, model="claude-opus-5-5", usage=USAGE)


@pytest.fixture
def usage(tmp_path):
    return JsonlUsageLog(tmp_path / "usage.jsonl")


def test_cost_is_computed_from_tokens_and_unknown_models_are_priced_high():
    assert cost_usd(USAGE, "claude-opus-5-5") == pytest.approx((1_000 * 4 + 500 * 20) / 1e6)
    assert cost_usd(USAGE, "some-other-model") > cost_usd(USAGE, "claude-opus-5-5")


def test_each_call_is_recorded_and_the_budget_stops_further_calls(usage):
    assistant = AiAssistant(FakeModel(), usage, AiBudget(monthly_usd=0.02))
    assistant.explain("質問", {}, TODAY)
    assert len(usage.records()) == 1
    assert assistant.spent_this_month(TODAY) == pytest.approx(0.014)
    assistant.explain("質問", {}, TODAY)  # 0.014 < 0.02 なので、まだ呼べる
    with pytest.raises(AiError, match="上限"):
        assistant.explain("質問", {}, TODAY)


def test_empty_question_is_rejected_before_calling(usage):
    model = FakeModel()
    with pytest.raises(AiError, match="質問"):
        AiAssistant(model, usage).explain("  ", {}, TODAY)
    assert not model.calls


def test_report_requires_the_structured_shape(usage):
    good = {"status": "normal", "summary": "問題なし", "anomalies": [], "next_actions": []}
    model = FakeModel(data=good)
    reply = AiAssistant(model, usage).daily_report({"equity": 10_000}, TODAY)
    assert reply.data == good
    assert model.calls[0]["schema"] is REPORT_SCHEMA and model.calls[0]["effort"] == EFFORT["report"]

    with pytest.raises(AiError, match="決まった形"):
        AiAssistant(FakeModel(data=None), usage).daily_report({}, TODAY)


def decision(**kwargs):
    base = {"action": "buy", "symbol": "7203", "quantity": 3, "confidence": "low", "reasoning": "理由", "risks": "弱点"}
    base.update(kwargs)
    return base


PORTFOLIO = {"cash": 10_000, "positions": [{"symbol": "9432", "quantity": 5, "value": 750}]}
CANDIDATES = [{"symbol": "7203"}, {"symbol": "6758"}]


def decide(usage, data, mode=Mode.PAPER):
    return AiAssistant(FakeModel(data=data), usage).decide(
        mode=mode, as_of=TODAY, portfolio=PORTFOLIO, candidates=CANDIDATES, today=TODAY
    )


def test_decisions_are_only_for_paper_trading(usage):
    with pytest.raises(AiError, match="ペーパー"):
        decide(usage, decision(), mode=Mode.SIGNAL)


def test_a_valid_buy_becomes_one_order_tagged_as_ai(usage):
    _, orders, rejected = decide(usage, decision())
    assert not rejected
    (order,) = orders
    assert (order.symbol, order.side.value, order.quantity) == ("7203", "buy", 3.0)
    assert order.reason.startswith("AI判断")


@pytest.mark.parametrize(
    "data, why",
    [
        (decision(symbol="9999"), "候補に無い"),
        (decision(action="sell", symbol="7203"), "保有していない"),
        (decision(quantity=0), "数量"),
    ],
)
def test_unusable_decisions_are_rejected(usage, data, why):
    _, orders, rejected = decide(usage, data)
    assert not orders and why in rejected


def test_hold_produces_no_order_and_sells_are_capped_at_holdings(usage):
    assert decide(usage, decision(action="hold", symbol="", quantity=0))[1] == []
    _, orders, _ = decide(usage, decision(action="sell", symbol="9432", quantity=99))
    assert orders[0].quantity == 5


def test_research_context_keeps_the_latest_result_per_candidate():
    records = [
        {"kind": "dev", "label": "a", "sharpe_daily": 0.01, "total_return": 0.1},
        {"kind": "dev", "label": "a", "sharpe_daily": 0.02, "total_return": 0.2},
        {"kind": "dev", "label": "b", "sharpe_daily": 0.03},
        {"kind": "lockbox", "label": "a"},
    ]
    context = research_context(records)
    assert context["trials_recorded"] == 3 and context["lockbox_evaluations"] == 1
    assert [c["label"] for c in context["candidates"]] == ["b", "a"]
    assert context["candidates"][1]["total_return"] == 0.2


# ----------------------------------------------------------------------
# Claude の呼び出し（偽のクライアント）
# ----------------------------------------------------------------------
class FakeClient:
    def __init__(self, response=None, error=None):
        self.requests, self.response, self.error = [], response, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def response(text, stop="end_turn", category=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        stop_details=SimpleNamespace(category=category) if category else None,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


def test_request_sets_effort_schema_cache_and_fallback():
    client = FakeClient(response('{"action": "hold"}'))
    reply = ClaudeModel("key", client=client).ask(system="前提", prompt="質問", schema=DECISION_SCHEMA, effort="high")

    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": DECISION_SCHEMA}}
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["betas"] == [FALLBACK_BETA] and request["fallbacks"] == "default"
    assert "thinking" not in request  # Opus 5.5 は常に考える。無効にすると 400
    assert reply.data == {"action": "hold"} and reply.usage["input_tokens"] == 10


def test_refusal_and_truncation_are_reported():
    with pytest.raises(AiError, match="控えました.*cyber"):
        ClaudeModel("key", client=FakeClient(response("", stop="refusal", category="cyber"))).ask(system="s", prompt="p")
    with pytest.raises(AiError, match="切れました"):
        ClaudeModel("key", client=FakeClient(response("途中", stop="max_tokens"))).ask(system="s", prompt="p")


def test_invalid_json_gives_no_data():
    reply = ClaudeModel("key", client=FakeClient(response("not json"))).ask(system="s", prompt="p", schema={})
    assert reply.data is None


def test_api_errors_become_plain_messages_without_the_key():
    class AuthenticationError(Exception):
        pass

    with pytest.raises(AiError, match="APIキーが正しくありません") as info:
        ClaudeModel("sk-secret", client=FakeClient(error=AuthenticationError("bad sk-secret"))).ask(system="s", prompt="p")
    assert "sk-secret" not in str(info.value)
    assert "sk-secret" not in repr(ClaudeModel("sk-secret", client=FakeClient()))
