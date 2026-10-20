"""AI パーツ。本物の API は呼ばず、偽のモデル・偽のクライアントで約束を固定する。

  * 費用の上限を超えたら呼ばない。呼ぶたびに（失敗も）記録する
  * モデルは既定と役割ごとの上書きで選べ、保存できる。一覧に無いモデルも使える（高めに見積もる）
  * 決まった形で返らなければ使わない
  * D（売買判断）はペーパー運用でのみ。候補に無い銘柄・持っていない銘柄は使わない
  * Claude の呼び出しは、モデル・effort・構造化出力を付け、切り替えの無いモデルにはフォールバックを付けない
  * モデルごとの戦績: D で買った株を売ったときの損益を、先入れ先出しでモデルに結び付ける
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from jqbt.core.gates import Mode
from jqbt.extensions.ai.analytics import caveats, model_report, tag_of, trade_outcomes
from jqbt.extensions.ai.assistant import (
    DECISION_SCHEMA,
    EFFORT,
    REPORT_SCHEMA,
    AiAssistant,
    AiBudget,
    AiError,
    RefusalError,
    research_context,
)
from jqbt.extensions.ai.claude import FALLBACK_BETA, ClaudeModel
from jqbt.extensions.ai.gemini import GeminiModel
from jqbt.extensions.ai.models import (
    DEFAULT_MODEL,
    ModelCatalog,
    ModelChoice,
    ModelSettings,
    ModelSpec,
    cost_usd,
    normalize,
    spec,
)
from jqbt.extensions.ai.ports import ModelReply
from jqbt.extensions.ai.providers import ProviderRouter
from jqbt.infrastructure.secrets import EnvSecrets
from jqbt.extensions.ai.usage import JsonlUsageLog

TODAY = dt.date(2026, 10, 18)
USAGE = {"input_tokens": 1_000, "output_tokens": 500, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


class FakeModel:
    def __init__(self, data=None, text="答え", error=None, refuse=()):
        self.data, self.text, self.error, self.refuse, self.calls = data, text, error, set(refuse), []

    def ask(self, *, model, system, prompt, schema=None, effort="medium"):
        self.calls.append({"model": model, "system": system, "prompt": prompt, "schema": schema, "effort": effort})
        if model in self.refuse:
            raise RefusalError("AI が答えを控えました（分類: 不明）")
        if self.error:
            raise self.error
        return ModelReply(text=self.text, data=self.data if schema else None, model=model, usage=USAGE)


@pytest.fixture
def usage(tmp_path):
    return JsonlUsageLog(tmp_path / "usage.jsonl")


@pytest.fixture
def settings(tmp_path):
    return ModelSettings(tmp_path / "settings.json", environ={})


def assistant(model, usage, settings, budget=AiBudget()):
    return AiAssistant(model, usage, budget, settings)


# ----------------------------------------------------------------------
# モデルの一覧・料金・設定
# ----------------------------------------------------------------------
def test_default_is_claude_on_vertex_and_unknown_models_are_priced_at_the_top(tmp_path):
    catalog = ModelCatalog(tmp_path / "models.json")
    assert DEFAULT_MODEL == "vertex:claude-opus-5-5" and DEFAULT_MODEL in catalog.all()
    assert cost_usd(USAGE, DEFAULT_MODEL, catalog) == pytest.approx((1_000 * 4 + 500 * 20) / 1e6)
    top = max(cost_usd(USAGE, k, catalog) for k in catalog.all())
    assert cost_usd(USAGE, "vertex:some-future-model", catalog) == pytest.approx(top)
    assert not spec("vertex:some-future-model", catalog).server_fallback


def test_old_style_names_are_read_as_anthropic_direct():
    assert normalize("claude-opus-5-5") == "anthropic:claude-opus-5-5"
    assert normalize("vertex:gemini-3.8-flash") == "vertex:gemini-3.8-flash"


def test_models_can_be_added_and_bad_entries_are_rejected(tmp_path):
    catalog = ModelCatalog(tmp_path / "models.json")
    catalog.add(ModelSpec("vertex", "gemini-9-pro", "Gemini 9 Pro", "gemini", 1.0, 5.0, 0.1))
    assert catalog.all()["vertex:gemini-9-pro"].output == 5.0
    with pytest.raises(ValueError):
        catalog.add(ModelSpec("nowhere", "x", "x", "claude", 1, 1, 0))
    with pytest.raises(ValueError):
        catalog.add(ModelSpec("vertex", "x", "x", "claude", -1, 1, 0))


def test_settings_save_load_chain_and_env_override(tmp_path):
    store = ModelSettings(tmp_path / "s.json", environ={})
    store.save(ModelChoice(default="vertex:claude-sonnet-5-5", per_role={"decide": "vertex:claude-fable-5-1", "bogus": "x"},
                           fallback_chain=["vertex:gemini-3.8-flash", "vertex:claude-sonnet-5-5"]))
    loaded = store.load()
    assert loaded.model_for("explain") == "vertex:claude-sonnet-5-5"
    assert loaded.model_for("decide") == "vertex:claude-fable-5-1"
    assert "bogus" not in loaded.per_role
    # 既定と同じモデルは、切り替えの順番に二度は出てこない
    assert loaded.attempts("explain") == ["vertex:claude-sonnet-5-5", "vertex:gemini-3.8-flash"]
    env = ModelSettings(tmp_path / "s.json", environ={"AI_MODEL": "claude-haiku-5-5"}).load()
    assert env.default == "anthropic:claude-haiku-5-5"


def test_each_role_uses_its_configured_model(usage, settings):
    settings.save(ModelChoice(default="vertex:claude-sonnet-5-5", per_role={"report": "vertex:claude-haiku-5-5"}))
    model = FakeModel(data={"status": "normal", "summary": "", "anomalies": [], "next_actions": []})
    ai = assistant(model, usage, settings)
    ai.explain("質問", {}, TODAY)
    ai.daily_report({}, TODAY)
    assert [c["model"] for c in model.calls] == ["vertex:claude-sonnet-5-5", "vertex:claude-haiku-5-5"]


def test_a_refusal_moves_on_to_the_next_model_in_the_chain(usage, settings):
    settings.save(ModelChoice(default="vertex:claude-opus-5-5", fallback_chain=["vertex:gemini-3.8-flash"]))
    model = FakeModel(refuse={"vertex:claude-opus-5-5"})
    reply = assistant(model, usage, settings).explain("質問", {}, TODAY)
    assert reply.model == "vertex:gemini-3.8-flash"
    first, second = usage.records()
    assert (first["model"], first["ok"]) == ("vertex:claude-opus-5-5", False)
    assert (second["model"], second["ok"]) == ("vertex:gemini-3.8-flash", True)


def test_when_every_model_refuses_the_user_sees_which_were_tried(usage, settings):
    settings.save(ModelChoice(default="vertex:claude-opus-5-5", fallback_chain=["vertex:gemini-3.8-flash"]))
    model = FakeModel(refuse={"vertex:claude-opus-5-5", "vertex:gemini-3.8-flash"})
    with pytest.raises(RefusalError, match="claude-opus-5-5 → vertex:gemini-3.8-flash"):
        assistant(model, usage, settings).explain("質問", {}, TODAY)


def test_other_errors_do_not_try_the_next_model(usage, settings):
    settings.save(ModelChoice(default="vertex:claude-opus-5-5", fallback_chain=["vertex:gemini-3.8-flash"]))
    model = FakeModel(error=AiError("認証に失敗しました"))
    with pytest.raises(AiError, match="認証"):
        assistant(model, usage, settings).explain("質問", {}, TODAY)
    assert len(model.calls) == 1


# ----------------------------------------------------------------------
# 呼び出し先への振り分け
# ----------------------------------------------------------------------
class Recorder:
    def __init__(self, name):
        self.name, self.calls = name, []

    def ask(self, **kwargs):
        self.calls.append(kwargs)
        return ModelReply(text="ok", data=None, model=kwargs["model"], usage=USAGE)


def test_router_sends_each_key_to_its_provider_and_reports_back_the_key(tmp_path):
    vc, vg, ac = Recorder("vertex-claude"), Recorder("vertex-gemini"), Recorder("anthropic-claude")
    router = ProviderRouter(
        environ={"GCP_PROJECT": "p"}, catalog=ModelCatalog(tmp_path / "m.json"),
        factories={("vertex", "claude"): lambda: vc, ("vertex", "gemini"): lambda: vg, ("anthropic", "claude"): lambda: ac},
    )
    assert router.ask(model="vertex:claude-opus-5-5", system="s", prompt="p").model == "vertex:claude-opus-5-5"
    router.ask(model="vertex:gemini-3.8-flash", system="s", prompt="p")
    router.ask(model="anthropic:claude-opus-5-5", system="s", prompt="p")
    assert vc.calls[0]["model"] == "claude-opus-5-5" and "server_fallback" not in vc.calls[0]
    assert vg.calls[0]["model"] == "gemini-3.8-flash"
    assert ac.calls[0]["server_fallback"] is True  # Anthropic 直接の Opus 5.5 だけサーバー側の切り替えを使う


def test_readiness_explains_what_is_missing_without_calling_the_api(tmp_path):
    catalog = ModelCatalog(tmp_path / "m.json")
    ok, why = ProviderRouter(environ={}, catalog=catalog).readiness("vertex:claude-opus-5-5")
    assert not ok and "GCP_PROJECT" in why
    assert ProviderRouter(environ={"GCP_PROJECT": "p"}, catalog=catalog).readiness("vertex:claude-opus-5-5") == (True, "")
    ok, why = ProviderRouter(environ={}, secrets=EnvSecrets({}), catalog=catalog).readiness("anthropic:claude-opus-5-5")
    assert not ok and "APIキー" in why
    ok, why = ProviderRouter(environ={}, catalog=catalog).readiness("anthropic:gemini-3.8-flash")
    assert not ok and "Claude だけ" in why


# ----------------------------------------------------------------------
# 呼び出しの記録・費用の上限・評価
# ----------------------------------------------------------------------
def test_each_call_is_recorded_and_the_budget_stops_further_calls(usage, settings):
    ai = assistant(FakeModel(), usage, settings, AiBudget(monthly_usd=0.02))
    reply = ai.explain("質問", {}, TODAY)
    assert reply.call_id and usage.records()[0]["model"] == DEFAULT_MODEL
    assert ai.spent_this_month(TODAY) == pytest.approx(0.014)
    ai.explain("質問", {}, TODAY)
    with pytest.raises(AiError, match="上限"):
        ai.explain("質問", {}, TODAY)


def test_failures_are_recorded_for_the_model_comparison(usage, settings):
    ai = assistant(FakeModel(error=AiError("AI が答えを控えました（分類: cyber）")), usage, settings)
    with pytest.raises(AiError):
        ai.explain("質問", {}, TODAY)
    (record,) = usage.records()
    assert record["ok"] is False and "控えました" in record["error"] and record["cost_usd"] == 0.0


def test_ratings_are_tied_to_the_call_and_its_model(usage, settings):
    ai = assistant(FakeModel(), usage, settings)
    reply = ai.explain("質問", {}, TODAY)
    ai.rate(reply.call_id, 4)
    rating = usage.records()[-1]
    assert rating["kind"] == "rating" and rating["model"] == DEFAULT_MODEL and rating["score"] == 4
    with pytest.raises(AiError):
        ai.rate(reply.call_id, 9)
    with pytest.raises(AiError):
        ai.rate("missing", 3)


def test_ratings_do_not_count_as_spending(usage, settings):
    ai = assistant(FakeModel(), usage, settings)
    reply = ai.explain("質問", {}, TODAY)
    before = ai.spent_this_month(TODAY)
    ai.rate(reply.call_id, 5)
    assert ai.spent_this_month(TODAY) == pytest.approx(before)


def test_empty_question_is_rejected_before_calling(usage, settings):
    model = FakeModel()
    with pytest.raises(AiError, match="質問"):
        assistant(model, usage, settings).explain("  ", {}, TODAY)
    assert not model.calls


def test_report_requires_the_structured_shape(usage, settings):
    good = {"status": "normal", "summary": "問題なし", "anomalies": [], "next_actions": []}
    model = FakeModel(data=good)
    reply = assistant(model, usage, settings).daily_report({"equity": 10_000}, TODAY)
    assert reply.data == good
    assert model.calls[0]["schema"] is REPORT_SCHEMA and model.calls[0]["effort"] == EFFORT["report"]
    with pytest.raises(AiError, match="決まった形"):
        assistant(FakeModel(data=None), usage, settings).daily_report({}, TODAY)
    assert usage.records()[-1]["ok"] is False


# ----------------------------------------------------------------------
# D 売買判断
# ----------------------------------------------------------------------
def decision(**kwargs):
    base = {"action": "buy", "symbol": "7203", "quantity": 3, "confidence": "low", "reasoning": "理由", "risks": "弱点"}
    base.update(kwargs)
    return base


PORTFOLIO = {"cash": 10_000, "positions": [{"symbol": "9432", "quantity": 5, "value": 750}]}
CANDIDATES = [{"symbol": "7203"}, {"symbol": "6758"}]


def decide(usage, settings, data, mode=Mode.PAPER):
    return assistant(FakeModel(data=data), usage, settings).decide(
        mode=mode, as_of=TODAY, portfolio=PORTFOLIO, candidates=CANDIDATES, today=TODAY
    )


def test_decisions_are_only_for_paper_trading(usage, settings):
    with pytest.raises(AiError, match="ペーパー"):
        decide(usage, settings, decision(), mode=Mode.SIGNAL)


def test_a_valid_buy_becomes_one_order_and_the_decision_is_recorded(usage, settings):
    _, orders, rejected = decide(usage, settings, decision())
    assert not rejected
    (order,) = orders
    assert (order.symbol, order.side.value, order.quantity) == ("7203", "buy", 3.0)
    assert usage.records()[0]["decision"]["action"] == "buy"


@pytest.mark.parametrize(
    "data, why",
    [
        (decision(symbol="9999"), "候補に無い"),
        (decision(action="sell", symbol="7203"), "保有していない"),
        (decision(quantity=0), "数量"),
    ],
)
def test_unusable_decisions_are_rejected(usage, settings, data, why):
    _, orders, rejected = decide(usage, settings, data)
    assert not orders and why in rejected


def test_hold_produces_no_order_and_sells_are_capped_at_holdings(usage, settings):
    assert decide(usage, settings, decision(action="hold", symbol="", quantity=0))[1] == []
    _, orders, _ = decide(usage, settings, decision(action="sell", symbol="9432", quantity=99))
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


# ----------------------------------------------------------------------
# モデルごとの戦績
# ----------------------------------------------------------------------
def signal(id_, reason):
    return {"id": id_, "reason": reason}


def fill(id_, date, side, symbol, quantity, price, signal_id=None, fee=0.0):
    return {"id": id_, "date": date, "side": side, "symbol": symbol, "quantity": quantity,
            "price": price, "signal_id": signal_id, "fee": fee}


def test_tags_identify_ai_signals_only():
    assert tag_of("[AI:vertex:claude-opus-5-5:abc123def456] AI判断") == ("vertex:claude-opus-5-5", "abc123def456")
    assert tag_of("[AI:claude-opus-5-5:abc123] AI判断") == ("claude-opus-5-5", "abc123")
    assert tag_of("regime_hold_50") is None
    assert tag_of(None) is None


def test_profits_follow_the_model_that_bought_first_in_first_out():
    signals = [signal(1, "[AI:opus:aaaaaa] 買い"), signal(2, "[AI:haiku:bbbbbb] 買い"), signal(3, "戦略の売り")]
    fills = [
        fill(1, "2026-01-05", "buy", "X", 10, 100, signal_id=1),   # opus が買う
        fill(2, "2026-01-06", "buy", "X", 10, 110, signal_id=2),   # haiku が買う
        fill(3, "2026-01-07", "sell", "X", 15, 120, signal_id=3),  # 先に opus の10、次に haiku の5
    ]
    out = trade_outcomes(signals, fills)
    assert out["opus"]["pnls"] == [pytest.approx((120 - 100) * 10)]
    assert out["haiku"]["pnls"] == [pytest.approx((120 - 110) * 5)]
    assert out["haiku"]["open_quantity"] == pytest.approx(5)


def test_fees_reduce_the_attributed_profit():
    signals = [signal(1, "[AI:opus:aaaaaa] 買い")]
    fills = [fill(1, "2026-01-05", "buy", "X", 10, 100, 1, fee=10), fill(2, "2026-01-06", "sell", "X", 10, 100, fee=10)]
    assert trade_outcomes(signals, fills)["opus"]["pnls"] == [pytest.approx(-20)]


def test_model_report_combines_calls_ratings_and_trades_and_warns_on_small_samples():
    records = [
        {"kind": "call", "call_id": "aaaaaa", "model": "vertex:opus", "role": "decide", "ok": True, "latency_ms": 2000,
         "cost_usd": 0.02, "decision": {"action": "buy", "confidence": "low"}},
        {"kind": "call", "call_id": "cccccc", "model": "vertex:opus", "role": "explain", "ok": False,
         "error": "AI が答えを控えました", "latency_ms": 500, "cost_usd": 0.0},
        {"kind": "rating", "call_id": "aaaaaa", "model": "vertex:opus", "score": 4},
    ]
    signals = [signal(1, "[AI:vertex:opus:aaaaaa] 買い")]
    fills = [fill(1, "2026-01-05", "buy", "X", 10, 100, 1), fill(2, "2026-01-06", "sell", "X", 10, 105)]
    (row,) = model_report(records, signals, fills)
    assert row["calls"] == 2 and row["success_rate"] == 0.5 and row["refusals"] == 1
    assert row["rating"] == 4 and row["actions"]["buy"] == 1 and row["trades"] == 1
    assert row["pnl_total"] == pytest.approx(50) and row["win_rate"] == 1.0
    notes = " ".join(caveats([row]))
    assert "30件未満" in notes and "学習した期間" in notes


# ----------------------------------------------------------------------
# Claude の呼び出し（偽のクライアント）
# ----------------------------------------------------------------------
class FakeClient:
    """beta（サーバー側の切り替えあり）と通常の両方の口を持つ。"""

    def __init__(self, response=None, error=None):
        self.requests, self.response, self.error = [], response, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **r: self._create("beta", r)))
        self.messages = SimpleNamespace(create=lambda **r: self._create("plain", r))

    def _create(self, path, request):
        self.requests.append((path, request))
        if self.error:
            raise self.error
        return self.response


def response(text, stop="end_turn", category=None, model="claude-opus-5-5"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        stop_details=SimpleNamespace(category=category) if category else None,
        model=model,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


def test_anthropic_direct_uses_effort_schema_cache_and_server_fallback():
    client = FakeClient(response('{"action": "hold"}'))
    reply = ClaudeModel(client=client, server_fallback=True).ask(
        model="claude-opus-5-5", system="前提", prompt="質問", schema=DECISION_SCHEMA, effort="high"
    )
    path, request = client.requests[0]
    assert path == "beta" and request["betas"] == [FALLBACK_BETA] and request["fallbacks"] == "default"
    assert request["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": DECISION_SCHEMA}}
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in request
    assert reply.data == {"action": "hold"} and reply.usage["input_tokens"] == 10


def test_vertex_style_calls_have_no_server_fallback():
    client = FakeClient(response("ok"))
    ClaudeModel(client=client, server_fallback=False).ask(model="claude-opus-5-5", system="s", prompt="p")
    path, request = client.requests[0]
    assert path == "plain" and "fallbacks" not in request and "betas" not in request


def test_refusal_and_truncation_are_reported():
    with pytest.raises(RefusalError, match="控えました.*cyber"):
        ClaudeModel(client=FakeClient(response("", stop="refusal", category="cyber"))).ask(model="m", system="s", prompt="p")
    with pytest.raises(AiError, match="切れました"):
        ClaudeModel(client=FakeClient(response("途中", stop="max_tokens"))).ask(model="m", system="s", prompt="p")


def test_invalid_json_gives_no_data():
    reply = ClaudeModel(client=FakeClient(response("not json"))).ask(model="m", system="s", prompt="p", schema={})
    assert reply.data is None


def test_vertex_quota_and_data_sharing_errors_are_explained():
    class RateLimitError(Exception):
        pass

    class PermissionDeniedError(Exception):
        pass

    quota = RateLimitError("Quota exceeded for aiplatform.googleapis.com/... base model: anthropic-claude-opus")
    with pytest.raises(AiError, match="クォータ"):
        ClaudeModel(client=FakeClient(error=quota)).ask(model="m", system="s", prompt="p")
    sharing = PermissionDeniedError("Access to this model requires data sharing to be enabled for publisher 'anthropic'")
    with pytest.raises(AiError, match="データ共有"):
        ClaudeModel(client=FakeClient(error=sharing)).ask(model="m", system="s", prompt="p")


def test_api_errors_become_plain_messages_without_the_key():
    class AuthenticationError(Exception):
        pass

    with pytest.raises(AiError, match="認証に失敗") as info:
        ClaudeModel(client=FakeClient(error=AuthenticationError("bad sk-secret"))).ask(model="m", system="s", prompt="p")
    assert "sk-secret" not in str(info.value)
    assert "sk-secret" not in repr(ClaudeModel(client=FakeClient()))


# ----------------------------------------------------------------------
# Gemini の呼び出し（偽のクライアント）
# ----------------------------------------------------------------------
class FakeGenai:
    def __init__(self, result=None, error=None):
        self.requests, self.result, self.error = [], result, error
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.result


def gemini_result(text="{}", finish="STOP", block=None):
    return SimpleNamespace(
        text=text,
        prompt_feedback=SimpleNamespace(block_reason=block) if block else None,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
        usage_metadata=SimpleNamespace(prompt_token_count=100, cached_content_token_count=40,
                                       candidates_token_count=20, thoughts_token_count=30),
    )


def test_gemini_requests_json_and_thinking_level_and_counts_thoughts_as_output():
    client = FakeGenai(gemini_result('{"action": "hold"}'))
    reply = GeminiModel(client=client).ask(model="gemini-3.8-flash", system="前提", prompt="質問",
                                           schema=DECISION_SCHEMA, effort="high")
    config = client.requests[0]["config"]
    assert config["response_mime_type"] == "application/json" and config["response_json_schema"] is DECISION_SCHEMA
    assert config["thinking_config"] == {"thinking_level": "HIGH"} and config["system_instruction"] == "前提"
    assert reply.data == {"action": "hold"}
    assert reply.usage == {"input_tokens": 60, "output_tokens": 50, "cache_read_input_tokens": 40,
                           "cache_creation_input_tokens": 0}


@pytest.mark.parametrize("result", [gemini_result(block=SimpleNamespace(name="SAFETY")), gemini_result(finish="SAFETY")])
def test_gemini_safety_blocks_are_refusals(result):
    with pytest.raises(RefusalError):
        GeminiModel(client=FakeGenai(result)).ask(model="g", system="s", prompt="p")


def test_gemini_errors_are_explained_by_status_code():
    class APIError(Exception):
        code = 404

    with pytest.raises(AiError, match="Model Garden"):
        GeminiModel(client=FakeGenai(error=APIError())).ask(model="g", system="s", prompt="p")
