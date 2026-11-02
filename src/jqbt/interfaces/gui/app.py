"""運用の操作盤。表示と入力だけを行い、判断は application / core に任せる。

**注文は出さない。** 発注は人が行い、約定をここで記録する。
出力は検証ツールの結果であって、投資助言ではない。
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from jqbt.application.fx_backtest import FxConfig, run_fx
from jqbt.application.operations import OperationsError, OperationsService, StaleDataError
from jqbt.application.panel_index import build_index
from jqbt.core.fx.model import FxCosts, FxInstrument
from jqbt.core.gates import Mode
from jqbt.core.risk import RiskLimits
from jqbt.infrastructure.env import load_env
from jqbt.infrastructure.fx_synthetic import REGIMES, generate
from jqbt.infrastructure.ledger import Ledger, kill_path, path_for
from jqbt.infrastructure.secrets import SECRETS, SecretError, build_secrets
from jqbt.interfaces import viewmodels as vm
from jqbt.interfaces.composition import DataRequest, load_panel
from jqbt.interfaces.extensions import GuiContext, enabled_names, load
from jqbt.strategies import REGISTRY
from jqbt.strategies.fx import BollingerRevert, EmaCross, RandomFx, RangeBreakout

ROOT = Path(__file__).resolve().parents[4]
DISCLAIMER = "検証ツールの出力であり、投資助言ではありません。発注は人が行います。結果は保証されません。"
STOCK_STRATEGIES = ["regime_hold", "momentum", "reversal", "lowvol", "breakout"]
FX_STRATEGIES = {
    "EMA交差（順張り）": lambda seed: EmaCross(),
    "ボリンジャー逆張り": lambda seed: BollingerRevert(),
    "レンジブレイク": lambda seed: RangeBreakout(),
    "ランダム（基準線）": lambda seed: RandomFx(seed=seed),
}

load_env()


@st.cache_resource
def extensions():
    """拡張パーツ（JQBT_EXTENSIONS で有効にしたもの）。本体はパーツを名前でだけ知っている。"""
    return load(enabled_names())
st.set_page_config(
    page_title="Algo Trading",
    page_icon=":material/candlestick_chart:",
    layout="wide",
    initial_sidebar_state="auto",
)

# 見た目の微調整。標準のメニューとフッターを隠し、余白と数字のカードをそろえる（配色は .streamlit/config.toml）
st.markdown(
    """
    <style>
      #MainMenu, footer, [data-testid="stToolbarActions"] {visibility: hidden;}
      .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px;}
      h1 {font-size: 1.6rem !important; font-weight: 700 !important; letter-spacing: -0.01em;}
      h2, h3 {font-weight: 650 !important; letter-spacing: -0.01em;}
      [data-testid="stMetric"] {background: var(--secondary-background-color); border: 1px solid rgba(15,23,42,.08);
                                 border-radius: 12px; padding: 14px 16px;}
      [data-testid="stMetricLabel"] p {font-size: .8rem; color: #64748b;}
      [data-testid="stMetricValue"] {font-size: 1.45rem; font-variant-numeric: tabular-nums;}
      [data-testid="stDataFrame"] {border-radius: 10px; overflow: hidden;}
      .page-subtitle {color: #64748b; margin-top: -0.6rem; margin-bottom: 1.2rem; font-size: .92rem;}
      .pill {display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: .75rem; font-weight: 600;}
      .pill.ok {background: #dcfce7; color: #166534;} .pill.warn {background: #fef3c7; color: #92400e;}
      .pill.bad {background: #fee2e2; color: #991b1b;} .pill.info {background: #e0e7ff; color: #3730a3;}
    </style>
    """,
    unsafe_allow_html=True,
)


def header(title: str, subtitle: str = "") -> None:
    """画面の見出しと、ひとことの説明。"""
    st.title(title)
    if subtitle:
        st.markdown(f'<div class="page-subtitle">{subtitle}</div>', unsafe_allow_html=True)


def pill(text: str, kind: str = "info") -> str:
    return f'<span class="pill {kind}">{text}</span>'


# ----------------------------------------------------------------------
# 資源（重いものは一度だけ作って使い回す）
# ----------------------------------------------------------------------
@st.cache_resource(show_spinner="相場データを読み込んでいます…")
def market():
    panel = load_panel(DataRequest(synthetic=False, start=None, end=None, symbols=None, regime="mixed"))
    return panel, build_index(panel)


@st.cache_resource
def ledger(profile: str) -> Ledger:
    return Ledger(path_for(profile))


def service(profile: str) -> OperationsService:
    panel, index = market()
    book = ledger(profile)

    def factory(params):
        return REGISTRY[book.get_state("strategy") or "regime_hold"].factory(params)

    return OperationsService(book, panel, factory, lot_size=1, index=index)


def git_commit() -> tuple[str, bool]:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "src", "scripts"], cwd=ROOT,
                                    capture_output=True, text=True, check=True).stdout.strip())
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        # Docker の中には Git が無い。イメージを作るときに渡した版を使う（make docker-build）
        version = os.environ.get("JQBT_VERSION", "unknown")
        return version.split("+")[0], "+" in version


@st.cache_data(ttl=300, show_spinner=False)
def secret_overview() -> list[str]:
    """秘密情報の置き場所と、有無だけ。値は画面に出さない（5分ごとに確かめ直す）。"""
    try:
        source = build_secrets()
        lines = [f"秘密情報: {source.describe()}"]
        for name, spec in SECRETS.items():
            try:
                state = vm.secret_state(source.get(name))
            except SecretError:
                state = "読めません（ログインと権限を確認）"
            lines.append(f"・{spec.description}: {state}")
        return lines
    except SecretError as exc:
        return [f"秘密情報: 設定の誤り（{exc}）"]


def now_text() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def flash(message: str, kind: str = "success") -> None:
    """操作の結果を、画面の再描画をまたいで1回だけ出す。"""
    st.session_state["flash"] = (kind, message)
    st.rerun()


def equity_chart(series: pd.Series) -> None:
    """評価額の推移。縦軸を0から始めると、1万円前後の動きが見えなくなるので、値の範囲に合わせる。"""
    frame = series.rename("評価額").rename_axis("時刻").reset_index()
    chart = alt.Chart(frame).mark_line().encode(
        x=alt.X("時刻:T", title=None),
        y=alt.Y("評価額:Q", scale=alt.Scale(zero=False), title="評価額（円）"),
    )
    st.altair_chart(chart, width="stretch")


def cards(items: list[dict[str, str]], per_row: int = 2) -> None:
    """数字のカード。幅が狭いと値が切れるので、2列ずつ並べる。"""
    for start in range(0, len(items), per_row):
        row = items[start : start + per_row]
        for column, item in zip(st.columns(per_row), row):
            column.metric(item["label"], item["value"], item.get("note") or None, delta_color="off")



# ----------------------------------------------------------------------
# 国内株の画面が使うもの（相場データの読み込みは、国内株の画面を開いたときだけ）
# ----------------------------------------------------------------------
class StockContext:
    def __init__(self, ops, latest, index, fresh, profile):
        self.ops, self.latest, self.index, self.fresh, self.profile = ops, latest, index, fresh, profile


def stock() -> StockContext:
    profile = st.session_state.get("profile", "paper")
    try:
        panel, index = market()
        latest = index.dates[-1]
    except Exception as exc:  # データが無いときも、ほかの画面は使える
        st.error(f"国内株の相場データを読み込めません: {exc}。先に `make fetch` でデータを取得してください。")
        st.stop()
    ops = service(profile)
    ops.apply_pending(dt.datetime.now())
    fresh = vm.freshness(latest, dt.date.today())
    if kill_path(profile).exists():
        st.error("キルスイッチが入っています（売買候補を出しません）", icon=":material/block:")
    return StockContext(ops, latest, index, fresh, profile)


def show_flash() -> None:
    if "flash" in st.session_state:
        kind, message = st.session_state.pop("flash")
        getattr(st, kind)(message)


# ----------------------------------------------------------------------
# 概要（ホーム）
# ----------------------------------------------------------------------
def fx_events() -> list[dict]:
    from jqbt.infrastructure import fx_paper_log

    return fx_paper_log.load()


def account_rows(events: list[dict]) -> list[dict]:
    from jqbt.infrastructure import fx_paper_log

    rows = []
    for name, state in fx_paper_log.latest_snapshots(events).items():
        closes = [e for e in events if e.get("kind") == "close" and e.get("account") == name]
        position = state.get("position")
        equity = state.get("equity") or state["cash"]
        rows.append({
            "口座": vm.account_label(name),
            "id": name,
            "種類": vm.account_kind(name),
            "評価額": round(equity),
            "損益": round(equity - state["initial"]),
            "損益率": f"{equity / state['initial'] - 1:+.2%}",
            "建玉": f"{position['symbol']} {position['units']:+,.0f}" if position else "—",
            "閉じた取引": len(closes),
            "勝ち": sum(e["pnl"] > 0 for e in closes),
            "停止": "ロスカット" if state.get("stopped") else "",
        })
    return rows


def page_home() -> None:
    header("概要", "FX のペーパー運用（仮想の約定）の今と、システムの状態。注文は出していません")
    events = fx_events()
    rows = account_rows(events)
    if not rows:
        st.info("まだ記録がありません。ペーパー運用が始まると、ここに口座の成績が出ます", icon=":material/hourglass_empty:")
        return
    by_name = {r["id"]: r for r in rows}
    runs = sorted({e["at"][:16] for e in events if e.get("kind") == "snapshot"})
    trades = sum(r["閉じた取引"] for r in rows)
    best = max(rows, key=lambda r: r["損益"])
    ai = by_name.get("ai")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("AI の口座", f"{ai['評価額']:,}円" if ai else "—", f"{ai['損益']:+,}円" if ai else None)
    c2.metric("一番良い口座", best["口座"], f"{best['損益']:+,}円")
    c3.metric("閉じた取引（全口座）", f"{trades} 件")
    c4.metric(f"最後の実行（これまで {len(runs)} 回）", runs[-1][5:16].replace("T", " ") if runs else "—")

    left, right = st.columns([3, 2], gap="large")
    with left:
        with st.container(border=True):
            st.markdown("**評価額の推移**")
            curve = pd.DataFrame([{"時刻": pd.Timestamp(e["at"]), "口座": vm.account_label(e["account"]),
                                   "評価額": e["state"].get("equity") or e["state"]["cash"]}
                                  for e in events if e.get("kind") == "snapshot"])
            if len(curve) > 1:
                chart = (alt.Chart(curve).mark_line(strokeWidth=2)
                         .encode(x=alt.X("時刻:T", title=None),
                                 y=alt.Y("評価額:Q", scale=alt.Scale(zero=False), title=None),
                                 color=alt.Color("口座:N", legend=alt.Legend(orient="bottom", title=None)))
                         .properties(height=300))
                st.altair_chart(chart, width="stretch")
            else:
                st.caption("2回以上の実行で線になります")
    with right:
        with st.container(border=True):
            st.markdown("**口座**")
            table = pd.DataFrame(rows)[["口座", "種類", "損益", "建玉"]].sort_values("損益", ascending=False)
            st.dataframe(table, hide_index=True, width="stretch",
                         column_config={"損益": st.column_config.NumberColumn(format="%+d 円")})
    with st.container(border=True):
        st.markdown("**最近の判断**")
        recent = [e for e in events if e.get("kind") == "decision"
                  and (e.get("decision") or {}).get("action") not in (None, "hold")][-8:]
        if not recent:
            st.caption("まだ売買の判断はありません（見送りだけ）")
        for e in reversed(recent):
            d = e.get("decision") or {}
            ok = pill("通過", "ok") if e.get("approved") else pill("却下", "bad")
            who = pill("AI", "info") if e["account"] == "ai" else pill("機械", "warn")
            why = d.get("reasoning") or ", ".join(f"{k}={v}" for k, v in (e.get("logic") or {}).items()
                                                 if k not in ("rule", "version"))
            st.markdown(f"{who} {ok} `{e['at'][5:16].replace('T', ' ')}` **{vm.account_label(e['account'])}** — "
                        f"{d.get('action')} {d.get('symbol', '')}  \n<span style='color:#64748b;font-size:.85rem'>"
                        f"{(why or '')[:160]}</span>", unsafe_allow_html=True)


# ----------------------------------------------------------------------
# FX
# ----------------------------------------------------------------------
def page_fx_paper() -> None:
    header("ペーパー運用", "AI・機械（ルール）・基準の口座を、同じ時刻・同じ値段で並べた仮想の運用。注文は出していません")
    events = fx_events()
    if not events:
        st.info("まだ記録がありません。scripts/fx_paper.py を実行すると始まります（Cloud Run では1時間に1回）",
                icon=":material/hourglass_empty:")
        return
    rows = account_rows(events)
    groups = {kind: [r for r in rows if r["種類"] == kind] for kind in ("AI", "機械", "基準")}
    c1, c2, c3 = st.columns(3)
    for column, kind in zip((c1, c2, c3), ("AI", "機械", "基準")):
        group = groups[kind]
        pnl = sum(r["損益"] for r in group)
        column.metric(f"{kind}（{len(group)} 口座）の損益の合計・閉じた取引 {sum(r['閉じた取引'] for r in group)} 件",
                      f"{pnl:+,} 円")
    with st.container(border=True):
        st.markdown("**口座**")
        table = pd.DataFrame(rows).drop(columns=["id"])
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "評価額": st.column_config.NumberColumn(format="%d 円"),
            "損益": st.column_config.NumberColumn(format="%+d 円"),
        })
    with st.container(border=True):
        st.markdown("**評価額の推移**")
        curve = pd.DataFrame([{"時刻": pd.Timestamp(e["at"]), "口座": vm.account_label(e["account"]),
                               "評価額": e["state"].get("equity") or e["state"]["cash"]}
                              for e in events if e.get("kind") == "snapshot"])
        if len(curve) > 1:
            chart = (alt.Chart(curve).mark_line(strokeWidth=2)
                     .encode(x=alt.X("時刻:T", title=None), y=alt.Y("評価額:Q", scale=alt.Scale(zero=False), title=None),
                             color=alt.Color("口座:N", legend=alt.Legend(orient="bottom", title=None)))
                     .properties(height=320))
            st.altair_chart(chart, width="stretch")
    def kind_of(account: str) -> str:
        if account == "ai":
            return "AI"
        return "基準" if account in ("buy_and_hold", "no_trade") else "機械"

    def result_of(e: dict) -> str:
        return "通過" if e.get("approved") else (e.get("rejected") or e.get("error") or "見送り")

    decisions = [e for e in events if e.get("kind") == "decision"]
    ai_tab, rule_tab, fill_tab = st.tabs(["AI の判断", "機械（ルール）の判断", "約定と手仕舞い"])
    with ai_tab:
        ai_rows = [{
            "時刻": e.get("at", "")[:16].replace("T", " "),
            "判断": (e.get("decision") or {}).get("action", "エラー" if "error" in e else ""),
            "通貨": (e.get("decision") or {}).get("symbol", ""),
            "リスク": (e.get("decision") or {}).get("risk_fraction"),
            "損切り": (e.get("decision") or {}).get("stop_pips"),
            "利確": (e.get("decision") or {}).get("take_pips"),
            "確信度": (e.get("decision") or {}).get("confidence", ""),
            "結果": result_of(e),
            "根拠": (e.get("decision") or {}).get("reasoning", "") or e.get("error", ""),
            "前提文": e.get("prompt_version") or "fx-v1",
            "モデル": e.get("model", ""),
        } for e in reversed(decisions) if e.get("account") == "ai"][:100]
        if ai_rows:
            counts = pd.Series([r["判断"] for r in ai_rows]).value_counts()
            st.caption("直近の判断の内訳: " + " / ".join(f"{k} {v}" for k, v in counts.items()))
            st.dataframe(pd.DataFrame(ai_rows), hide_index=True, width="stretch")
        else:
            st.caption("まだありません")
    with rule_tab:
        rule_rows = []
        for e in reversed(decisions):
            if e.get("account") == "ai":
                continue
            logic = e.get("logic") or {}
            rule_rows.append({
                "時刻": e.get("at", "")[:16].replace("T", " "),
                "口座": vm.account_label(e.get("account", "")),
                "種類": kind_of(e.get("account", "")),
                "判断": (e.get("decision") or {}).get("action", ""),
                "結果": result_of(e),
                "見た数字": ", ".join(f"{k}={v}" for k, v in logic.items() if k not in ("rule", "version")),
                "版": logic.get("version", ""),
            })
        rule_rows = rule_rows[:200]
        if rule_rows:
            st.dataframe(pd.DataFrame(rule_rows), hide_index=True, width="stretch")
            with st.expander("各ルールの決まり"):
                seen = {}
                for e in decisions:
                    if (e.get("logic") or {}).get("rule"):
                        seen[e["account"]] = e["logic"]["rule"]
                for account, rule in seen.items():
                    st.markdown(f"- **{vm.account_label(account)}**: {rule}")
        else:
            st.caption("まだありません")
    with fill_tab:
        fills = [e for e in events if e.get("kind") in ("open", "close", "halted", "stopped")]
        st.dataframe(pd.DataFrame([{
            "時刻": e.get("at", "")[:16].replace("T", " "), "口座": vm.account_label(e.get("account", "")),
            "種類": {"open": "建てた", "close": "閉じた", "halted": "1日の上限で停止", "stopped": "ロスカット"}[e["kind"]],
            "通貨": e.get("symbol", ""), "数量": e.get("units"),
            "値段": round(e.get("price", e.get("exit_price", 0)) or 0, 3),
            "損益": round(e["pnl"]) if "pnl" in e else None,
            "理由": str(e.get("reason", ""))[:120],
        } for e in reversed(fills)][:200]), hide_index=True, width="stretch")
    st.caption("判断のあとの値動きと根拠の照らし合わせは scripts/fx_paper_review.py。値動きは結果論なので、傾向を見る")



def page_fx_lab() -> None:
    header("FX の検証（ダミーデータ）", "仕組みの確認用。ダミーデータの成績は、勝てる根拠になりません")
    st.warning("ダミーデータの結果です。仕組みの検証であり、実際の相場で勝てることを示すものではありません。")
    with st.form("fx"):
        col1, col2, col3, col4 = st.columns(4)
        regime = col1.selectbox("値動きの性質", REGIMES, format_func=lambda r: {
            "random": "random（性質なし）", "meanrev": "meanrev（平均回帰）", "trend": "trend（トレンド）",
            "jumps": "jumps（急変・窓開け）"}[r])
        strategy_name = col2.selectbox("戦略", list(FX_STRATEGIES))
        days = col3.slider("営業日数", 5, 60, 20)
        seed = col4.number_input("乱数の種", 0, 10_000, 0)
        col5, col6, col7, col8 = st.columns(4)
        cash = col5.number_input("資金（円）", 1_000, 10_000_000, 10_000, step=1_000)
        units = col6.number_input("1回の数量（通貨）", 1, 100_000, 100, step=100)
        min_units = col7.number_input("最小取引単位（通貨）", 1, 10_000, 1)
        spread = col8.number_input("スプレッド（pips）", 0.0, 10.0, 0.2, step=0.1)
        losscut = st.slider("ロスカットの基準（証拠金維持率）", 0.2, 1.0, 0.5, 0.05)
        submitted = st.form_submit_button("検証する", type="primary")
    if submitted:
        with st.spinner("計算しています…"):
            bars = generate(days=days, regime=regime, seed=int(seed))
            config = FxConfig(
                initial_cash=float(cash), units=float(units),
                instrument=FxInstrument(min_units=int(min_units), losscut_ratio=losscut),
                costs=FxCosts(spread_pips=spread),
            )
            result = run_fx(bars, FX_STRATEGIES[strategy_name](int(seed)), config)
        summary = result.summary
        cards(vm.fx_summary_cards(summary))
        if summary["losscut"]:
            st.error(f"強制ロスカット（{summary['losscut_at']}）。この条件での検証は、ここで終わりです")
        if summary["negative_balance"]:
            st.error("入金額を超える損失（不足金）が出ました")
        if summary["skipped_entries"]:
            st.warning(f"証拠金が足りず、{summary['skipped_entries']}回の取引を見送りました")
        equity_chart(pd.Series(result.equity, index=bars.index).resample("1h").last().dropna())
        if result.trades:
            trades = pd.DataFrame([t.__dict__ for t in result.trades])
            st.caption("手仕舞いの理由: " + " / ".join(f"{k} {v}回" for k, v in trades["exit_reason"].value_counts().items()))
            st.dataframe(trades.tail(200), hide_index=True, width="stretch")



# ----------------------------------------------------------------------
# 国内株
# ----------------------------------------------------------------------
def page_today() -> None:
    header("今日", "国内株の運用の状態と、増額の条件")
    ctx = stock()
    ops, latest, index, fresh, profile = ctx.ops, ctx.latest, ctx.index, ctx.fresh, ctx.profile
    show_flash()
    if not ops.initialized:
        st.subheader("始める")
        st.write("戦略とパラメータを **凍結** して始めます。運用中は変えられません。変えるなら新しい台帳で始め直します。")
        with st.form("init"):
            cash = st.number_input("資金（円）", min_value=1_000, value=10_000, step=1_000)
            strategy = st.selectbox("戦略", STOCK_STRATEGIES)
            spec = REGISTRY[strategy]
            params = {key: st.selectbox(key, values) for key, values in spec.grid.items()}
            params["max_positions"] = st.number_input("同時に持てる銘柄数", 1, 10, 5)
            agree = st.checkbox("戦略を凍結し、運用中は変えないことを理解した")
            submitted = st.form_submit_button("凍結して始める")
        if submitted:
            if not agree:
                st.error("凍結の確認にチェックしてください")
            else:
                commit, dirty = git_commit()
                ops.init(
                    cash=float(cash), strategy=strategy, params=params,
                    version=f"{commit}{'+未コミットの変更あり' if dirty else ''}", limits=RiskLimits(),
                    mode=Mode.PAPER if profile == "paper" else Mode.SIGNAL,
                    started_on=dt.date.today(), at=now_text(),
                )
                flash(f"始めました（{strategy} を凍結）")
    else:
        status = ops.status(latest)
        cards(vm.status_cards(status))
        commit, _ = git_commit()
        frozen = (status["version"] or "").split("+")[0]
        if frozen and frozen != commit:
            st.warning(f"凍結した版（{frozen}）と現在のコード（{commit}）が違います。候補は別の戦略の出力かもしれません")
        if profile == "live" and not fresh["ok"]:
            st.warning("相場データが古いため、実運用の売買候補は出せません（J-Quants の Free プランは12週間遅れです）")
        left, right = st.columns(2)
        with left:
            st.subheader("保有")
            rows = vm.position_rows(status["positions"])
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            else:
                st.caption("保有はありません")
            st.caption(f"戦略 {status['strategy']} {status['params']} / 凍結した版 {status['version']} / 最後の入れ替え {status['last_rebalance'] or '—'}")
        with right:
            st.subheader("増額の条件（全部満たすまで資金を増やさない）")
            st.dataframe(pd.DataFrame(vm.gate_rows(status["gates"])), hide_index=True, width="stretch")
        if status["planned"]:
            st.info(f"未約定の候補が {len(status['planned'])} 件あります（「売買候補と約定」で記録）")



def page_signals() -> None:
    header("売買候補と約定", "売買候補を出し、人が発注した約定を記録する")
    ctx = stock()
    ops, latest, index, fresh, profile = ctx.ops, ctx.latest, ctx.index, ctx.fresh, ctx.profile
    show_flash()
    if not ops.initialized:
        st.info("先に「今日」で始めてください")
    else:
        st.subheader("売買候補")
        st.caption(f"{latest} の引け後の判断。執行は翌営業日の寄り。{DISCLAIMER}")
        if st.button(f"{latest} の候補を作る", type="primary"):
            if kill_path(profile).exists():
                st.error("キルスイッチが入っています。「設定」で再開するまで候補は出ません")
            else:
                try:
                    ops.mark(latest)
                    result = ops.plan(latest, today=dt.date.today())
                    if result.already:
                        st.info("この日の候補は作成済みです")
                    if result.signals:
                        st.dataframe(pd.DataFrame(vm.signal_rows(result.signals)), hide_index=True, width="stretch")
                    else:
                        st.caption("候補はありません" + ("" if result.due else "（今日は入れ替え日ではありません）"))
                    for text, why in result.rejected:
                        st.warning(f"見送り {text}: {why}")
                except StaleDataError as exc:
                    st.error(f"候補を出せません: {exc}")
                except OperationsError as exc:
                    st.error(f"できません: {exc}")

        st.subheader("約定を記録する")
        planned = ops.ledger.signals("planned")
        choices = ["（候補から選ばずに入力）"] + [vm.signal_choice(s) for s in planned]
        picked = st.selectbox("候補", choices)
        signal = planned[choices.index(picked) - 1] if picked != choices[0] else None
        with st.form("fill"):
            col1, col2, col3 = st.columns(3)
            day = col1.date_input("約定日", dt.date.today())
            symbol = col2.text_input("銘柄", signal["symbol"] if signal else "")
            side = col3.selectbox("売買", ["buy", "sell"], index=0 if not signal or signal["side"] == "buy" else 1,
                                  format_func=lambda s: "買い" if s == "buy" else "売り")
            col4, col5, col6 = st.columns(3)
            quantity = col4.number_input("数量", min_value=0.0, value=float(signal["quantity"]) if signal else 0.0, step=1.0)
            price = col5.number_input("約定価格（円）", min_value=0.0, value=0.0, step=0.1, format="%.2f")
            fee = col6.number_input("手数料（円）", min_value=0.0, value=0.0, step=1.0)
            note = st.text_input("メモ", "")
            submitted = st.form_submit_button("記録する")
        if submitted:
            if not symbol.strip():
                st.error("銘柄を入力してください")
            else:
                try:
                    record = ops.record_fill(day=day, symbol=symbol.strip(), side=side, quantity=quantity, price=price,
                                             fee=fee, signal_id=signal["id"] if signal else None, note=note)
                    ops.mark(min(day, latest))
                    message = f"記録しました: {day} {symbol} {quantity:g} @ {price:,.2f}円"
                    if record["ref_open"]:
                        diff = price / record["ref_open"] - 1
                        diff = diff if side == "buy" else -diff
                        message += f"（始値 {record['ref_open']:,.2f}円とのずれ {diff:+.2%}）"
                    else:
                        message += "（この日の始値はデータに無く、ずれは後で測ります）"
                    flash(message)
                except OperationsError as exc:
                    st.error(f"記録できません: {exc}")

        if profile == "paper":
            st.subheader("ペーパーで過去を再生する")
            with st.form("replay"):
                col1, col2 = st.columns(2)
                start = col1.date_input("開始", index.dates[max(len(index.dates) - 200, 0)])
                end = col2.date_input("終了", latest)
                submitted = st.form_submit_button("再生する")
            if submitted:
                try:
                    with st.spinner("再生しています…"):
                        outcome = ops.replay(start, end)
                    flash(f"{start} 〜 {end} を再生しました（{outcome['days']}営業日、初回の約定 {outcome['first_fill']}）")
                except OperationsError as exc:
                    st.error(f"できません: {exc}")



def page_settings() -> None:
    header("設定", "実行モード・リスク制限・キルスイッチ。緩める変更は24時間後に効く")
    ctx = stock()
    ops, latest, index, fresh, profile = ctx.ops, ctx.latest, ctx.index, ctx.fresh, ctx.profile
    show_flash()
    if not ops.initialized:
        st.info("先に「今日」で始めてください")
    else:
        left, right = st.columns(2)
        with left:
            st.subheader("実行モード")
            options = vm.mode_options()
            current = ops.mode
            choice = st.radio(
                "モード", options, index=[o["mode"] for o in options].index(current),
                format_func=lambda o: o["label"] + ("" if o["available"] else "（未実装）"),
            )
            if not choice["available"]:
                st.caption(choice["note"])
            reason = st.text_input("理由（監査ログに残ります）", key="mode_reason")
            if st.button("モードを変える"):
                if not reason.strip():
                    st.error("理由を入力してください")
                else:
                    try:
                        ops.set_mode(choice["mode"], reason=reason, at=now_text())
                        flash(f"モードを {choice['label']} にしました")
                    except OperationsError as exc:
                        st.error(f"できません: {exc}")

            st.subheader("キルスイッチ")
            if kill_path(profile).exists():
                st.error(f"停止中: {kill_path(profile).read_text(encoding='utf-8')}")
                resume_reason = st.text_input("再開の理由", key="resume_reason")
                if st.button("再開する"):
                    if not resume_reason.strip():
                        st.error("理由を入力してください")
                    else:
                        kill_path(profile).unlink(missing_ok=True)
                        ops.set_mode(Mode.PAPER if profile == "paper" else Mode.SIGNAL,
                                     reason=f"再開: {resume_reason}", at=now_text())
                        flash("再開しました")
            else:
                kill_reason = st.text_input("止める理由", key="kill_reason")
                if st.button("全停止", type="primary"):
                    if not kill_reason.strip():
                        st.error("理由を入力してください")
                    else:
                        kill_path(profile).parent.mkdir(parents=True, exist_ok=True)
                        kill_path(profile).write_text(kill_reason, encoding="utf-8")
                        ops.set_mode(Mode.STOP, reason=f"キルスイッチ: {kill_reason}", at=now_text())
                        flash("停止しました。候補は出ません。約定の記録はできます", "warning")

        with right:
            st.subheader("リスク制限")
            limits = ops.limits
            col1, col2 = st.columns(2)
            new = RiskLimits(
                max_order_value=col1.number_input(vm.LIMIT_LABELS["max_order_value"], 0.0, value=float(limits.max_order_value), step=500.0),
                max_position_share=col2.number_input(vm.LIMIT_LABELS["max_position_share"], 0.0, 1.0, float(limits.max_position_share), 0.05),
                max_positions=int(col1.number_input(vm.LIMIT_LABELS["max_positions"], 1, 20, int(limits.max_positions))),
                max_daily_loss=col2.number_input(vm.LIMIT_LABELS["max_daily_loss"], 0.0, 1.0, float(limits.max_daily_loss), 0.01),
            )
            change = vm.limit_change(limits, new)
            st.dataframe(pd.DataFrame(change["rows"]), hide_index=True, width="stretch")
            (st.warning if change["loosening"] else st.caption)(change["timing"])
            limit_reason = st.text_input("理由（監査ログに残ります）", key="limit_reason")
            if st.button("制限を変える", disabled=not change["changed"]):
                if not limit_reason.strip():
                    st.error("理由を入力してください")
                else:
                    outcome = ops.request_limits(new, reason=limit_reason, now=dt.datetime.now())
                    flash("すぐ適用しました" if outcome == "applied" else "24時間後に有効になります", "success" if outcome == "applied" else "warning")
            pending = ops.ledger.get_state("pending_limits", "")
            if pending:
                st.info("緩める変更が待機中です（24時間後に有効）")



def page_history() -> None:
    header("履歴", "約定・売買候補・設定変更の記録（消せない）")
    ctx = stock()
    ops, latest, index, fresh, profile = ctx.ops, ctx.latest, ctx.index, ctx.fresh, ctx.profile
    show_flash()
    if not ops.initialized:
        st.info("先に「今日」で始めてください")
    else:
        curve = ops.ledger.equity_curve()
        if curve:
            st.subheader("評価額の推移")
            frame = pd.DataFrame(curve, columns=["日付", "評価額", "現金"])
            equity_chart(pd.Series(frame["評価額"].to_numpy(), index=pd.to_datetime(frame["日付"])))
        st.subheader("約定")
        fills = vm.fill_rows(ops.ledger.fills())
        if fills:
            st.dataframe(pd.DataFrame(fills), hide_index=True, width="stretch")
        else:
            st.caption("まだありません")
        st.subheader("売買候補")
        signals = vm.signal_rows(ops.ledger.signals())
        if signals:
            st.dataframe(pd.DataFrame(signals), hide_index=True, width="stretch")
        else:
            st.caption("まだありません")
        st.subheader("設定変更の履歴（監査ログ）")
        st.dataframe(pd.DataFrame(vm.audit_rows(ops.ledger.audit_log(200))), hide_index=True, width="stretch")



# ----------------------------------------------------------------------
# システム
# ----------------------------------------------------------------------
def page_system() -> None:
    header("システム", "版・秘密情報・拡張パーツ・データの状態")
    commit, dirty = git_commit()
    loaded = extensions()
    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        st.markdown("**版**")
        st.markdown(f"`{commit}`" + (" " + pill("未コミットの変更あり", "warn") if dirty else ""), unsafe_allow_html=True)
        st.markdown("**拡張パーツ**")
        st.write(", ".join(ext.title for ext in loaded.extensions) or "なし")
        for name, why in loaded.errors.items():
            st.warning(f"拡張パーツ {name} を読み込めません: {why}")
        st.markdown("**国内株の相場データ**")
        try:
            _, index = market()
            fresh = vm.freshness(index.dates[-1], dt.date.today())
            st.markdown(pill(fresh["message"], "ok" if fresh["ok"] else "warn"), unsafe_allow_html=True)
        except Exception as exc:  # データが無くても、システムの画面は出す
            st.caption(f"読み込めません: {exc}")
    with c2, st.container(border=True):
        st.markdown("**秘密情報**（値は表示しません）")
        for line in secret_overview():
            st.caption(line)
    st.caption(DISCLAIMER)


# ----------------------------------------------------------------------
# ナビゲーション
# ----------------------------------------------------------------------
def extension_page(render, title: str):
    def page() -> None:
        ctx = stock()
        header(title)
        render(GuiContext(profile=ctx.profile, ops=ctx.ops, index=ctx.index, latest=ctx.latest, today=dt.date.today()))

    page.__name__ = "ext_" + str(abs(hash(title)))
    return page


ext_pages = [st.Page(extension_page(page.render, page.title), title=page.title, icon=":material/smart_toy:",
                     url_path="ext-" + str(i)) for i, page in enumerate(p for ext in extensions().extensions
                                                                       for p in ext.gui_pages())]
navigation = st.navigation({
    "": [st.Page(page_home, title="概要", icon=":material/dashboard:", default=True)],
    "FX": [st.Page(page_fx_paper, title="ペーパー運用", icon=":material/show_chart:", url_path="fx-paper"),
           st.Page(page_fx_lab, title="検証（ダミー）", icon=":material/science:", url_path="fx-lab")],
    "国内株": [st.Page(page_today, title="今日", icon=":material/today:", url_path="stocks"),
            st.Page(page_signals, title="売買候補と約定", icon=":material/receipt_long:", url_path="signals"),
            st.Page(page_settings, title="設定", icon=":material/tune:", url_path="settings"),
            st.Page(page_history, title="履歴", icon=":material/history:", url_path="history")],
    **({"AI": ext_pages} if ext_pages else {}),
    "システム": [st.Page(page_system, title="状態", icon=":material/info:", url_path="system")],
})

with st.sidebar:
    st.selectbox("国内株の台帳", ["paper", "live"], key="profile",
                 format_func=lambda p: "ペーパー（仮想の約定）" if p == "paper" else "実運用（実際の約定を記録）")
    st.caption(DISCLAIMER)

navigation.run()
