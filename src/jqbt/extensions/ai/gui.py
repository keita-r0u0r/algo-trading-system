"""AI パーツの画面（Streamlit）。本体の GUI からは ``GuiPage`` として呼ばれる。

AI を呼ぶのは、ボタンを押したときだけ（画面を開いただけでは呼ばない）。1回ごとの費用を表示する。
"""

from __future__ import annotations

from dataclasses import asdict

import pandas as pd
import streamlit as st

from ...application.backtest import PanelMarketView
from ...core.gates import Mode
from ...infrastructure import experiment_log
from ...interfaces.extensions import GuiContext
from . import AiExtension, build_assistant
from .assistant import AiError, cost_usd, research_context, shortlist, status_context

SECTIONS = ["A 質問する", "B 今日の要約", "C 売買ルールの案", "D 売買判断（ペーパー）"]
NOTICE = "AI の出力は検証の補助であり、投資助言ではありません。AI に発注・設定変更の権限はありません。"


@st.cache_resource
def _assistant():
    return build_assistant()


def _cost_line(reply) -> None:
    usage = reply.usage
    st.caption(
        f"モデル {reply.model} / 入力 {usage.get('input_tokens', 0):,} / 出力 {usage.get('output_tokens', 0):,} "
        f"/ キャッシュ読み込み {usage.get('cache_read_input_tokens', 0):,} トークン / 約 ${cost_usd(usage, reply.model):.4f}"
    )


def _ops_context(ctx: GuiContext) -> dict:
    ops = ctx.ops
    if not ops.initialized:
        return {"note": "運用はまだ始まっていません"}
    return status_context(
        ops.status(ctx.latest), ops.stats(), ops.ledger.equity_curve(), ops.ledger.fills(), ops.ledger.audit_log(20)
    )


def render(ctx: GuiContext) -> None:
    st.info(NOTICE)
    ok, reason = AiExtension().available()
    if not ok:
        st.warning(f"AI を使う準備ができていません: {reason}")
        st.caption("設定の手順は docs/ai.md を参照してください。")
        return

    try:
        assistant = _assistant()
    except Exception as exc:
        st.error(f"AI を準備できません: {exc}")
        return

    spent = assistant.spent_this_month(ctx.today)
    cap = assistant.budget.monthly_usd
    st.progress(min(spent / cap, 1.0) if cap else 1.0, text=f"今月の費用 ${spent:.2f} / 上限 ${cap:.2f}")

    section = st.radio("使い方", SECTIONS, horizontal=True, key="ai_section")
    st.divider()
    try:
        if section == SECTIONS[0]:
            _ask(ctx, assistant)
        elif section == SECTIONS[1]:
            _report(ctx, assistant)
        elif section == SECTIONS[2]:
            _ideas(ctx, assistant)
        else:
            _decide(ctx, assistant)
    except AiError as exc:
        st.error(str(exc))


def _ask(ctx: GuiContext, assistant) -> None:
    st.caption("検証の記録と運用の状況（集計した数字だけ）を AI に渡して、質問に答えてもらいます。")
    use = st.multiselect("渡す情報", ["運用の状況", "検証の記録"], default=["運用の状況", "検証の記録"])
    question = st.text_area("質問", placeholder="なぜ momentum の候補はランダムと区別がつかないのですか？")
    if st.button("聞く", type="primary"):
        context = {}
        if "運用の状況" in use:
            context["operations"] = _ops_context(ctx)
        if "検証の記録" in use:
            context["research"] = research_context(experiment_log.load())
        with st.spinner("AI が考えています…"):
            st.session_state["ai_answer"] = assistant.explain(question, context, ctx.today)
    reply = st.session_state.get("ai_answer")
    if reply:
        st.markdown(reply.text)
        _cost_line(reply)


def _report(ctx: GuiContext, assistant) -> None:
    st.caption("運用の状況を要約し、異常を指摘します。")
    if not ctx.ops.initialized:
        st.info("運用がまだ始まっていません。「今日」の画面で始めてください。")
        return
    if st.button("今日の要約を作る", type="primary"):
        with st.spinner("AI が状況を読んでいます…"):
            st.session_state["ai_report"] = assistant.daily_report(_ops_context(ctx), ctx.today)
    reply = st.session_state.get("ai_report")
    if not reply:
        return
    report = reply.data
    show = {"normal": st.success, "caution": st.warning, "alert": st.error}[report["status"]]
    show(report["summary"])
    if report["anomalies"]:
        st.dataframe(
            pd.DataFrame(report["anomalies"]).rename(columns={"severity": "重さ", "title": "内容", "detail": "詳細"}),
            hide_index=True, width="stretch",
        )
    for action in report["next_actions"]:
        st.markdown(f"- {action}")
    _cost_line(reply)


def _ideas(ctx: GuiContext, assistant) -> None:
    st.caption(
        "これまでの検証の記録を踏まえて、売買ルールの案を出します。**案は試行ではありません。** "
        "実装して検証にかけたときに初めて試行回数 N に数えます。案を大量に出させて片端から試すと、偶然の当たりを拾います。"
    )
    market = st.selectbox("対象", ["jp_stock_daily", "fx_intraday"], format_func=lambda m: {
        "jp_stock_daily": "国内株（日足）", "fx_intraday": "FX（分足）"}[m])
    if st.button("案を出す", type="primary"):
        with st.spinner("AI が案を考えています…"):
            st.session_state["ai_ideas"] = assistant.propose(research_context(experiment_log.load()), market, ctx.today)
    reply = st.session_state.get("ai_ideas")
    if not reply:
        return
    for idea in reply.data["ideas"]:
        with st.expander(f"{idea['name']}（{idea['family']}）", expanded=True):
            st.markdown(f"**ルール**: {idea['rule']}")
            params = ", ".join(f"{p['name']}={p['values']}" for p in idea["parameters"])
            st.markdown(f"**パラメータの候補**: {params or '—'}")
            st.markdown(f"**理屈**: {idea['rationale']}")
            st.markdown(f"**弱点**: {idea['risks']}")
            st.markdown(f"**確かめ方**: {idea['test_plan']}")
    if reply.data.get("caution"):
        st.warning(reply.data["caution"])
    _cost_line(reply)


def _decide(ctx: GuiContext, assistant) -> None:
    ops = ctx.ops
    st.caption(
        "ペーパー運用でのみ、AI に売買の候補を1つ出させます。候補は既存のリスク制限を必ず通り、通ったものだけ台帳に残ります。"
    )
    st.warning(
        "AI は学習した期間の値動きを知っています。その期間の過去データでの判断は **先読み** になりうるため、"
        "評価に使えるのは、新しいデータで前向きに記録した結果だけです。"
    )
    if not ops.initialized:
        st.info("運用がまだ始まっていません。「今日」の画面で始めてください。")
        return
    if ops.mode is not Mode.PAPER:
        st.info("ペーパー運用（モード2）のときだけ使えます。今のモードでは使えません。")
        return

    status = ops.status(ctx.latest)
    max_positions = int(status["params"].get("max_positions", 5) or 5)
    budget = status["equity"] / max_positions
    view = PanelMarketView(ctx.index, ctx.latest, ops.lot_size)
    portfolio = {
        "as_of": ctx.latest,
        "cash": round(status["cash"]),
        "equity": round(status["equity"]),
        "budget_per_position": round(budget),
        "positions": [{"symbol": p["symbol"], "quantity": p["quantity"], "value": round(p["value"])} for p in status["positions"]],
        "limits": asdict(status["limits"]),
    }
    if st.button(f"{ctx.latest} の判断を出させる", type="primary"):
        candidates = shortlist(view, view.symbols(), budget)
        with st.spinner("AI が判断しています…"):
            reply, orders, rejected = assistant.decide(
                mode=ops.mode, as_of=ctx.latest, portfolio=portfolio, candidates=candidates, today=ctx.today
            )
        outcome = ops.submit_orders(ctx.latest, orders, source="AI") if orders else None
        st.session_state["ai_decision"] = (reply, rejected, outcome)
    saved = st.session_state.get("ai_decision")
    if not saved:
        return
    reply, rejected, outcome = saved
    decision = reply.data
    st.markdown(
        f"**判断**: {decision['action']} {decision['symbol']} {decision['quantity']}株（確信度 {decision['confidence']}）"
    )
    st.markdown(f"**根拠**: {decision['reasoning']}")
    st.markdown(f"**弱点**: {decision['risks']}")
    if rejected:
        st.error(f"AI の判断は使えませんでした: {rejected}")
    if outcome:
        for signal in outcome.signals:
            st.success(f"台帳に記録しました: #{signal['id']} {signal['side']} {signal['symbol']} {signal['quantity']:g}株")
        for text, why in outcome.rejected:
            st.warning(f"リスク制限で見送り: {text}: {why}")
    _cost_line(reply)
