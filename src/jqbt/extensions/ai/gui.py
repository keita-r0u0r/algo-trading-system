"""AI パーツの画面（Streamlit）。本体の GUI からは ``GuiPage`` として呼ばれる。

AI を呼ぶのは、ボタンを押したときだけ（画面を開いただけでは呼ばない）。1回ごとの費用とモデルを表示する。
モデルは画面から切り替えられる（既定のモデルと、役割ごとの上書き）。答えには1〜5の評価を付けられる。
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
from .analytics import caveats, model_report
from .assistant import AiError, research_context, shortlist, status_context
from .models import CATALOG, ROLE_LABELS, ROLES, ModelChoice, cost_usd, spec

SECTIONS = ["A 質問する", "B 今日の要約", "C 売買ルールの案", "D 売買判断（ペーパー）", "モデルの比較"]
NOTICE = "AI の出力は検証の補助であり、投資助言ではありません。AI に発注・設定変更の権限はありません。"
OTHER = "（一覧に無いモデルを入力）"
SAME = "（既定と同じ）"


@st.cache_resource
def _assistant():
    return build_assistant()


def _footer(assistant, reply) -> None:
    """使ったモデル・費用と、評価の欄。"""
    usage = reply.usage
    st.caption(
        f"{spec(reply.model).label} / 入力 {usage.get('input_tokens', 0):,} / 出力 {usage.get('output_tokens', 0):,} "
        f"/ キャッシュ読み込み {usage.get('cache_read_input_tokens', 0):,} トークン / {reply.latency_ms / 1000:.1f}秒 "
        f"/ 約 ${cost_usd(usage, reply.model):.4f}"
    )
    if not reply.call_id:
        return
    rated = st.session_state.setdefault("ai_rated", set())
    if reply.call_id in rated:
        st.caption("評価を記録しました")
        return
    stars = st.feedback("stars", key=f"rate_{reply.call_id}")
    if stars is not None:
        assistant.rate(reply.call_id, int(stars) + 1)
        rated.add(reply.call_id)
        st.caption("評価を記録しました（モデルの比較に使います）")


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
    _model_settings(assistant)

    section = st.radio("使い方", SECTIONS, horizontal=True, key="ai_section")
    st.divider()
    try:
        if section == SECTIONS[0]:
            _ask(ctx, assistant)
        elif section == SECTIONS[1]:
            _report(ctx, assistant)
        elif section == SECTIONS[2]:
            _ideas(ctx, assistant)
        elif section == SECTIONS[3]:
            _decide(ctx, assistant)
        else:
            _compare(ctx, assistant)
    except AiError as exc:
        st.error(str(exc))


# ----------------------------------------------------------------------
# モデルの設定
# ----------------------------------------------------------------------
def _pick(label: str, current: str, key: str, allow_same: bool = False) -> str:
    """一覧から選ぶか、一覧に無いモデル名を入力する。"""
    options = ([SAME] if allow_same else []) + list(CATALOG) + [OTHER]
    if not current:
        index = 0
    elif current in CATALOG:
        index = options.index(current)
    else:
        index = options.index(OTHER)
    choice = st.selectbox(label, options, index=index, key=key,
                          format_func=lambda m: CATALOG[m].label if m in CATALOG else m)
    if choice == OTHER:
        return st.text_input(f"{label}（モデル名）", value="" if current in CATALOG else current, key=f"{key}_custom").strip()
    return "" if choice == SAME else choice


def _model_settings(assistant) -> None:
    choice = assistant.settings.load()
    with st.expander(f"モデルの設定（既定: {spec(choice.default).label}）"):
        st.dataframe(
            pd.DataFrame([
                {"モデル": m.label, "ID": m.id, "入力 $/100万": m.input, "出力 $/100万": m.output,
                 "断られたときの切り替え": "あり" if m.fallback else "なし", "特徴": m.note}
                for m in CATALOG.values()
            ]),
            hide_index=True, width="stretch",
        )
        st.caption("料金は2026年10月時点の公表値です。一覧に無いモデルは、最も高い料金で見積もり、切り替えは付けません。")
        default = _pick("既定のモデル", choice.default, "ai_default_model")
        st.caption("役割ごとに変えることもできます（例: 要約は安いモデル、判断は高性能なモデル）。")
        columns = st.columns(len(ROLES))
        per_role = {}
        for column, role in zip(columns, ROLES):
            with column:
                picked = _pick(ROLE_LABELS[role], choice.per_role.get(role, ""), f"ai_model_{role}", allow_same=True)
                if picked:
                    per_role[role] = picked
        if st.button("モデルの設定を保存"):
            if not default:
                st.error("既定のモデルを選んでください")
            else:
                assistant.settings.save(ModelChoice(default=default, per_role=per_role))
                st.success("保存しました。次の呼び出しから使います")


# ----------------------------------------------------------------------
# A〜D
# ----------------------------------------------------------------------
def _ask(ctx: GuiContext, assistant) -> None:
    st.caption(f"モデル: {spec(assistant.model_for('explain')).label}。検証の記録と運用の状況（集計した数字だけ）を渡して、質問に答えてもらいます。")
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
        _footer(assistant, reply)


def _report(ctx: GuiContext, assistant) -> None:
    st.caption(f"モデル: {spec(assistant.model_for('report')).label}。運用の状況を要約し、異常を指摘します。")
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
    _footer(assistant, reply)


def _ideas(ctx: GuiContext, assistant) -> None:
    st.caption(
        f"モデル: {spec(assistant.model_for('ideas')).label}。これまでの検証の記録を踏まえて、売買ルールの案を出します。"
        "**案は試行ではありません。** 実装して検証にかけたときに初めて試行回数 N に数えます。"
        "案を大量に出させて片端から試すと、偶然の当たりを拾います。"
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
    _footer(assistant, reply)


def _decide(ctx: GuiContext, assistant) -> None:
    ops = ctx.ops
    st.caption(
        f"モデル: {spec(assistant.model_for('decide')).label}。ペーパー運用でのみ、AI に売買の候補を1つ出させます。"
        "候補は既存のリスク制限を必ず通り、通ったものだけ台帳に残ります（どのモデルの判断かも記録します）。"
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
        source = f"AI:{reply.model}:{reply.call_id}"
        outcome = ops.submit_orders(ctx.latest, orders, source=source) if orders else None
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
    _footer(assistant, reply)


# ----------------------------------------------------------------------
# モデルの比較
# ----------------------------------------------------------------------
def _pct(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def _num(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def _compare(ctx: GuiContext, assistant) -> None:
    st.caption("記録（state/ai_usage.jsonl）と、ペーパー運用の台帳から、モデルごとの特徴と D の戦績を集計します。AI は呼びません。")
    ops = ctx.ops
    signals = ops.ledger.signals() if ops.initialized else []
    fills = ops.ledger.fills() if ops.initialized else []
    rows = model_report(assistant.usage.records(), signals, fills)
    if not rows:
        st.info("まだ記録がありません。A〜D を使うと、ここに集計が出ます。")
        return

    st.subheader("特徴")
    st.dataframe(pd.DataFrame([{
        "モデル": r["label"],
        "呼び出し": r["calls"],
        "成功率": _pct(r["success_rate"]),
        "断られた": r["refusals"],
        "平均の待ち時間(秒)": _num(r["mean_latency_s"], 1),
        "費用 合計($)": _num(r["cost_usd"], 4),
        "1回あたり($)": _num(r["cost_per_call_usd"], 4),
        "評価(1〜5)": f"{_num(r['rating'], 1)}（{r['ratings']}件）",
        "役割": ", ".join(f"{ROLE_LABELS.get(k, k)} {v}" for k, v in r["roles"].items()),
    } for r in rows]), hide_index=True, width="stretch")

    st.subheader("D 売買判断の戦績（ペーパー）")
    st.dataframe(pd.DataFrame([{
        "モデル": r["label"],
        "判断": r["decisions"],
        "買い/売り/見送り": f"{r['actions']['buy']}/{r['actions']['sell']}/{r['actions']['hold']}",
        "確信度 低/中/高": f"{r['confidence']['low']}/{r['confidence']['medium']}/{r['confidence']['high']}",
        "閉じた取引": r["trades"],
        "損益 合計(円)": _num(r["pnl_total"], 0),
        "1取引の平均(円)": _num(r["pnl_mean"], 0),
        "勝率": _pct(r["win_rate"]),
        "t値": _num(r["t_stat"]),
        "保有中(株)": _num(r["open_quantity"], 0),
    } for r in rows if r["decisions"] or r["trades"]]), hide_index=True, width="stretch")

    for note in caveats(rows):
        st.warning(note)
