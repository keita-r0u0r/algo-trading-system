"""運用の操作盤。表示と入力だけを行い、判断は application / core に任せる。

**注文は出さない。** 発注は人が行い、約定をここで記録する。
出力は検証ツールの結果であって、投資助言ではない。
"""

from __future__ import annotations

import datetime as dt
import json
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
from jqbt.application.fx_live_trade import LiveConfig
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


JST = dt.timezone(dt.timedelta(hours=9))
REFRESH_CHOICES = {"止める": None, "10秒": 10, "30秒": 30, "60秒": 60}


def refresh_seconds() -> int | None:
    """画面の自動更新の間隔（サイドバーで選ぶ）。"""
    return REFRESH_CHOICES.get(st.session_state.get("refresh", "30秒"))


def live(render):
    """表とグラフの部分だけを、選んだ間隔で描き直す（ページ全体は読み込み直さない）。"""
    return st.fragment(run_every=refresh_seconds())(render)


@st.cache_data(ttl=5, show_spinner=False)
def quotes() -> dict[str, dict[str, float]]:
    """今の値段（GMOコインの公開の値段。5秒ごとに取り直す）。取れなければ空。"""
    from jqbt.infrastructure.gmo_fx import GmoFxClient

    try:
        return {t["symbol"]: {"bid": float(t["bid"]), "ask": float(t["ask"])}
                for t in GmoFxClient().ticker() if t["symbol"] in vm.PAIR_LABELS}
    except Exception:  # 値段が取れなくても、記録は見せる
        return {}


def price_strip(prices: dict[str, dict[str, float]]) -> None:
    """今の値段の帯。"""
    if not prices:
        st.caption("今の値段を取れませんでした（記録の値で表示しています）")
        return
    columns = st.columns(len(prices))
    for column, (symbol, q) in zip(columns, prices.items()):
        mid = (q["bid"] + q["ask"]) / 2
        column.metric(vm.PAIR_LABELS.get(symbol, symbol), f"{mid:,.3f}",
                      f"スプレッド {(q['ask'] - q['bid']) / 0.01:.1f} pips", delta_color="off", delta_arrow="off")
    every = refresh_seconds()
    st.caption(f"{dt.datetime.now(JST):%H:%M:%S} 時点の値段" + (f"・{every}秒ごとに更新" if every else "・自動更新は止めています")
               + "。評価額と含み損益は、今の値段で評価し直しています（売買の判断と記録は15分ごと）")


def marked_equity(state: dict, prices: dict[str, dict[str, float]]) -> float:
    """今の値段で評価した口座の額（建玉の含み損益を含む。手仕舞いの値段＝買いは売値、売りは買値）。"""
    position = state.get("position")
    if not position or position["symbol"] not in prices:
        return state.get("equity") or state["cash"]
    q = prices[position["symbol"]]
    exit_price = q["bid"] if position["units"] > 0 else q["ask"]
    return state["cash"] + (exit_price - position["price"]) * position["units"]


def live_curve(events: list[dict], prices: dict[str, dict[str, float]]) -> pd.DataFrame:
    """評価額の推移。最後に「今」の点を足して、グラフを今の値段まで伸ばす。"""
    from jqbt.infrastructure import fx_paper_log

    curve = [{"時刻": pd.Timestamp(e["at"]), "口座": vm.account_label(e["account"]),
              "評価額": e["state"].get("equity") or e["state"]["cash"]}
             for e in events if e.get("kind") == "snapshot"]
    if prices and curve:
        now = pd.Timestamp(dt.datetime.now(JST))
        curve += [{"時刻": now, "口座": vm.account_label(name), "評価額": marked_equity(state, prices)}
                  for name, state in fx_paper_log.latest_snapshots(events).items()]
    return pd.DataFrame(curve)


def account_rows(events: list[dict], prices: dict[str, dict[str, float]] | None = None) -> list[dict]:
    from jqbt.infrastructure import fx_paper_log

    rows = []
    for name, state in fx_paper_log.latest_snapshots(events).items():
        closes = [e for e in events if e.get("kind") == "close" and e.get("account") == name]
        position = state.get("position")
        equity = marked_equity(state, prices or {})
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


def _old_page_home() -> None:
    header("概要", "FX のペーパー運用（仮想の約定）の今と、システムの状態")
    live(home_body)()


def home_body() -> None:
    prices = quotes()
    price_strip(prices)
    events = fx_events()
    rows = account_rows(events, prices)
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
            curve = live_curve(events, prices)
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
PAUSE_PAPER = ROOT / "state" / "PAUSE_fx_paper"


def paper_switch() -> None:
    """ペーパー運用の ON/OFF。止めている間は、判断も仮想の約定もしない（自動売買も新しく建てない）。"""
    running = not PAUSE_PAPER.exists()
    left, right = st.columns([3, 2])
    with left:
        st.markdown(pill("ペーパー運用: 動いている", "ok") if running else pill("ペーパー運用: 止めている", "warn"),
                    unsafe_allow_html=True)
        st.caption("止めている間は、15分ごとの回でペーパーの口座の判断も仮想の約定もしません。"
                   "実際の売買（自動売買）には影響しません")
    with right:
        if running:
            if st.button("ペーパー運用を止める", icon=":material/pause:", width="stretch"):
                PAUSE_PAPER.parent.mkdir(parents=True, exist_ok=True)
                PAUSE_PAPER.write_text(f"{dt.datetime.now(JST).isoformat(timespec='seconds')} 画面から止めた",
                                       encoding="utf-8")
                flash("ペーパー運用を止めました（次の回から）", "warning")
        elif st.button("ペーパー運用を再開する", type="primary", icon=":material/play_arrow:", width="stretch"):
            PAUSE_PAPER.unlink(missing_ok=True)
            flash("ペーパー運用を再開しました（次の回から）")


def page_fx_paper() -> None:
    header("ペーパー運用", "AI・機械（ルール）・基準の口座を、同じ時刻・同じ値段で並べた仮想の運用。注文は出していません")
    show_flash()
    paper_switch()
    live(fx_paper_body)()


def fx_paper_body() -> None:
    prices = quotes()
    price_strip(prices)
    events = fx_events()
    if not events:
        st.info("まだ記録がありません。scripts/fx_paper.py を実行すると始まります（Cloud Run では1時間に1回）",
                icon=":material/hourglass_empty:")
        return
    rows = account_rows(events, prices)
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
        curve = live_curve(events, prices)
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



def page_fx_live() -> None:
    header("実際の約定", "GMOコインの口座の約定・建玉・資産と、自動売買の記録")
    show_flash()
    live(fx_live_body)()
    fx_live_controls()


def fx_live_body() -> None:
    from jqbt.application.fx_live import summarize
    from jqbt.infrastructure import fx_live_log

    events = fx_live_log.load()
    if not events:
        st.info("まだ記録がありません。ペーパー運用と一緒に15分ごとに読みます（手元では scripts/fx_live_sync.py）",
                icon=":material/hourglass_empty:")
        return
    # 警告は、最後の回（最後の snapshot と同じ時刻）にエラーがあったときだけ出す
    last_at = next((e.get("at") for e in reversed(events) if e.get("kind") == "snapshot"), None)
    latest_errors = [x for e in events if e.get("kind") == "error" and e.get("at") == last_at for x in e["errors"]]
    if latest_errors:
        hints = []
        if any("ERR-5003" in x for x in latest_errors):
            hints.append("ERR-5003 は照会の呼びすぎ（一時的。次の回で読み直す）")
        if any("ERR-5012" in x for x in latest_errors):
            hints.append("ERR-5012 は APIキーの権限か IP の制限で弾かれている（GMOコインの API キーの設定を確かめる）")
        st.warning("最後の回で読めなかったもの: " + " / ".join(latest_errors) + ("。" + " ".join(hints) if hints else ""),
                   icon=":material/lock:")
    snapshots = [e for e in events if e.get("kind") == "snapshot"]
    last = snapshots[-1] if snapshots else {}
    assets = last.get("assets")
    assets = (assets[0] if isinstance(assets, list) and assets else assets) or {}
    executions = [e for e in events if e.get("kind") == "execution"]
    summary = summarize(executions)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("評価額", f"{float(assets.get('equity') or 0):,.0f} 円")
    c2.metric("確定した損益（手数料・スワップ込み）",
              f"{summary['loss_gain'] - summary['fee'] + summary['swap']:+,.0f} 円")
    c3.metric("約定", f"{summary['executions']} 件（決済 {summary['closed']}）")
    c4.metric("ずれの平均（不利な側が正）",
              f"{summary['slippage_avg']:+.2f} pips" if summary["slippage_avg"] is not None else "—")
    with st.container(border=True):
        st.markdown("**建玉**")
        positions = last.get("positions") or []
        if positions:
            st.dataframe(pd.DataFrame(positions), hide_index=True, width="stretch")
        else:
            st.caption("持っていません" if "positions" in last else "読めていません")
    with st.container(border=True):
        st.markdown("**約定**")
        if executions:
            st.dataframe(pd.DataFrame([{
                "時刻": e["at"][5:16].replace("T", " "), "通貨": e.get("symbol"), "売買": e.get("side"),
                "新規/決済": e.get("settleType"), "数量": float(e.get("size") or 0), "値段": float(e.get("price") or 0),
                "ずれ（pips）": e.get("slippage_pips"), "損益": float(e.get("lossGain") or 0),
                "手数料": float(e.get("fee") or 0), "スワップ": float(e.get("settledSwap") or 0),
            } for e in reversed(executions)]), hide_index=True, width="stretch")
        else:
            st.caption("まだありません")
    st.caption(f"最後に読んだ時刻: {last.get('at', '—')}。ずれは、約定した1分足の始まりの買値・売値との差（目安）")



def fx_live_controls() -> None:
    """自動売買の状態とキルスイッチ（ボタンがあるので、自動更新の外に置く）。"""
    st.subheader("自動売買")
    kill = ROOT / "state" / "KILL_fx_live"
    enabled = os.environ.get("FX_LIVE_ENABLED", "").lower() == "true"
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown(
            ("" if not enabled else "") +
            (pill("注文を出す設定", "ok") if enabled else pill("注文を出さない設定（記録だけ）", "info")) + " " +
            (pill("キルスイッチ: 入", "bad") if kill.exists() else pill("キルスイッチ: 切", "ok")),
            unsafe_allow_html=True)
        st.caption("実際に売買する判断する者: AI・金利差＋流れ（ドル円）。実際の口座の状態を見て判断し、状況に応じた数量（1口座1,000通貨まで）で、"
                   "損切りは GMOコイン側に置く。ペーパー運用とは独立。注文を出す設定は infra の fx_live_enabled")
        if kill.exists():
            st.caption(kill.read_text(encoding="utf-8"))
    with c2:
        if kill.exists():
            if st.button("キルスイッチを切る（再開）", icon=":material/play_arrow:"):
                kill.unlink(missing_ok=True)
                flash("キルスイッチを切りました")
        elif st.button("キルスイッチを入れる（新しい注文を止める）", type="primary", icon=":material/block:"):
            kill.parent.mkdir(parents=True, exist_ok=True)
            kill.write_text(f"{dt.datetime.now().isoformat(timespec='seconds')} 画面から止めた", encoding="utf-8")
            flash("キルスイッチを入れました。新しい注文は出しません（決済は出します）", "warning")
    live(fx_live_trades)()


def fx_live_trades() -> None:
    from jqbt.infrastructure import fx_live_log

    events = fx_live_log.load()
    live_events = [e for e in events if str(e.get("kind", "")).startswith("live_")]
    with st.container(border=True):
        st.markdown("**自動売買の記録**")
        if live_events:
            st.dataframe(pd.DataFrame([{
                "時刻": e.get("at", "")[5:16].replace("T", " "), "種類": e["kind"].removeprefix("live_"),
                "口座": vm.account_label(e.get("account", "")), "通貨": e.get("symbol", ""),
                "建玉ID": e.get("position_id"), "値段": e.get("price"),
                "内容": e.get("reason") or e.get("error") or e.get("note") or
                        (f"損切り {e.get('stop')} / 利確 {e.get('take')}" if e["kind"] == "live_protect" else ""),
            } for e in reversed(live_events)][:100]), hide_index=True, width="stretch")
        else:
            st.caption("まだありません")


# ----------------------------------------------------------------------
# 実運用 / ペーパーの切り替え（画面の上の「口座」）
# ----------------------------------------------------------------------
VENUES = {"実運用": "live", "ペーパー": "paper"}
KILL_LIVE = ROOT / "state" / "KILL_fx_live"


def venue() -> str:
    return VENUES.get(st.session_state.get("venue", "実運用"), "live")


def venue_header(title: str, live_text: str, paper_text: str) -> None:
    is_live = venue() == "live"
    header(title, live_text if is_live else paper_text)
    st.markdown(pill("実運用（実際のお金）", "bad") if is_live else pill("ペーパー（シミュレーション）", "info"),
                unsafe_allow_html=True)


def live_log() -> list[dict]:
    from jqbt.infrastructure import fx_live_log

    return fx_live_log.load()


def live_assets(snapshot: dict) -> dict:
    a = snapshot.get("assets")
    return (a[0] if isinstance(a, list) and a else a) or {}


def position_accounts(events: list[dict]) -> dict[int, str]:
    """建玉ID → その建玉を建てた判断する者。"""
    return {int(e["position_id"]): e["account"] for e in events
            if e.get("kind") == "live_open" and e.get("position_id")}


def live_positions(events: list[dict], prices: dict) -> list[dict]:
    """GMOコインの今の建玉（最後の照会）を、今の値段で評価し直す。"""
    from jqbt.application.fx_live_trade import book_from_events

    snapshots = [e for e in events if e.get("kind") == "snapshot"]
    positions = (snapshots[-1].get("positions") if snapshots else None) or []
    owners = position_accounts(events)
    book = {p.position_id: p for p in book_from_events(events).values()}
    rows = []
    for p in positions:
        pid, symbol, side = int(p["positionId"]), p["symbol"], p["side"]
        size, price = float(p["size"]), float(p["price"])
        q = prices.get(symbol)
        now = (q["bid"] if side == "BUY" else q["ask"]) if q else None
        sign = 1 if side == "BUY" else -1
        unrealized = (now - price) * size * sign if now is not None else float(p.get("lossGain") or 0)
        mine = book.get(pid)
        rows.append({"判断する者": vm.account_label(owners.get(pid, "")) or "（手で建てた）",
                     "通貨": vm.PAIR_LABELS.get(symbol, symbol), "売買": "買い" if side == "BUY" else "売り",
                     "数量": size, "建値": price, "今の値段": now, "含み損益": round(unrealized),
                     "損切り": mine.stop if mine else None, "利確": mine.take if mine else None,
                     "スワップ": float(p.get("totalSwap") or 0)})
    return rows


def live_closed(events: list[dict]) -> list[dict]:
    """決済した約定（確定した損益つき）。"""
    owners = position_accounts(events)
    rows = []
    for e in events:
        if e.get("kind") == "execution" and e.get("settleType") == "CLOSE":
            pnl = float(e.get("lossGain") or 0) + float(e.get("settledSwap") or 0) - float(e.get("fee") or 0)
            rows.append({"at": e["at"], "account": owners.get(int(e.get("positionId") or 0), ""), "pnl": pnl})
    return rows


def watcher_state() -> dict:
    """見張り役の生きている印（state/fx_watch.json）。"""
    path = ROOT / "state" / "fx_watch.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def watcher_pill() -> str:
    state = watcher_state()
    last = state.get("last_tick")
    if not last:
        return pill("見張り: 動いていない", "warn")
    age = (dt.datetime.now(JST) - dt.datetime.fromisoformat(last)).total_seconds()
    return pill(f"見張り: 動いている（{last[11:16]}）", "ok") if age < 300 else pill(f"見張り: 止まっている（{last[5:16].replace('T', ' ')}）", "bad")


def auto_trading_pills() -> str:
    enabled = os.environ.get("FX_LIVE_ENABLED", "").lower() == "true"
    return " ".join([watcher_pill(),
        pill("自動売買: ON", "ok") if enabled and not KILL_LIVE.exists() else pill("自動売買: 止めている", "warn"),
        pill("キルスイッチ: 入", "bad") if KILL_LIVE.exists() else pill("キルスイッチ: 切", "ok"),
        pill("ペーパー: 止めている", "warn") if PAUSE_PAPER.exists() else pill("ペーパー: 動いている", "info"),
    ])


def page_overview() -> None:
    venue_header("概要", "GMOコインの実際の口座。評価額・建玉・取引と、自動売買の状態",
                 "ペーパー運用（シミュレーション）の口座。実際のお金は動きません")
    live(live_overview if venue() == "live" else home_body)()


def live_overview() -> None:
    prices = quotes()
    price_strip(prices)
    events = live_log()
    snapshots = [e for e in events if e.get("kind") == "snapshot" and live_assets(e)]
    if not snapshots:
        st.info("まだ口座を読んだ記録がありません（15分ごとに読みます）", icon=":material/hourglass_empty:")
        return
    last = live_assets(snapshots[-1])
    positions = live_positions(events, prices)
    balance = float(last.get("balance") or last.get("equity") or 0)
    equity_now = balance + sum(r["含み損益"] for r in positions) if positions else float(last.get("equity") or 0)
    today = [live_assets(e) for e in snapshots if e["at"].startswith(dt.date.today().isoformat())]
    day_start = float(today[0].get("equity")) if today else equity_now
    closed = live_closed(events)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("評価額（今の値段）", f"{equity_now:,.0f} 円", f"今日 {equity_now - day_start:+,.0f} 円")
    c2.metric("確定した損益（累計）", f"{sum(r['pnl'] for r in closed):+,.0f} 円", f"決済 {len(closed)} 件",
              delta_color="off", delta_arrow="off")
    c3.metric("建玉", f"{len(positions)} 件", f"含み損益 {sum(r['含み損益'] for r in positions):+,} 円",
              delta_color="off", delta_arrow="off")
    ratio = float(last.get("marginRatio") or 0)
    c4.metric("取引余力", f"{float(last.get('availableAmount') or 0):,.0f} 円",
              f"証拠金維持率 {ratio:,.0f}%" if positions and 0 < ratio < 1e6 else "証拠金維持率 —（建玉なし）",
              delta_color="off", delta_arrow="off")
    st.markdown(auto_trading_pills(), unsafe_allow_html=True)

    left, right = st.columns([3, 2], gap="large")
    with left, st.container(border=True):
        st.markdown("**評価額の推移**")
        # 入金する前（評価額0）の記録は、グラフに入れない
        curve = pd.DataFrame([{"時刻": pd.Timestamp(e["at"]), "評価額": float(live_assets(e).get("equity") or 0)}
                              for e in snapshots if float(live_assets(e).get("equity") or 0) > 0])
        curve = pd.concat([curve, pd.DataFrame([{"時刻": pd.Timestamp(dt.datetime.now(JST)), "評価額": equity_now}])])
        if len(curve) > 1:
            st.altair_chart(alt.Chart(curve).mark_line(strokeWidth=2.5, point=True)
                            .encode(x=alt.X("時刻:T", title=None),
                                    y=alt.Y("評価額:Q", scale=alt.Scale(zero=False), title=None))
                            .properties(height=280), width="stretch")
    with right, st.container(border=True):
        st.markdown("**建玉**")
        if positions:
            st.dataframe(pd.DataFrame(positions)[["判断する者", "通貨", "売買", "数量", "建値", "含み損益"]],
                         hide_index=True, width="stretch",
                         column_config={"含み損益": st.column_config.NumberColumn(format="%+d 円")})
        else:
            st.caption("持っていません")
    with st.container(border=True):
        st.markdown("**判断する者ごとの今の状態**（見張り役が5分ごと・急変のときに判断。AI は15分ごと・急変のとき）")
        now_jst = dt.datetime.now(JST)
        market_open = bool(prices)
        latest: dict[str, tuple[dict, bool | None]] = {}
        for e, holding in zip(events, vm.holdings_over_time(events)):
            if e.get("kind") == "live_decision":
                latest[e["account"]] = (e, holding)
        owners_now = {r["判断する者"]: r for r in positions}
        rows = []
        for name in LiveConfig().accounts:
            e, holding = latest.get(name, ({}, None))
            d = e.get("decision") or {}
            health = vm.decider_health(events, name, now_jst, market_open=market_open)
            if health["state"] == "bad":
                st.error(f"{vm.account_label(name)}: {health['text']}")
            held = owners_now.get(vm.account_label(name))
            rows.append({
                "判断する者": vm.account_label(name),
                "動き": {"ok": "🟢 ", "warn": "🟡 ", "bad": "🔴 "}.get(health["state"], "⚪ ") + health["text"],
                "最後の判断": (e.get("at", "")[5:16].replace("T", " ") + " " + vm.action_label(d, holding)) if e else "—",
                "理由": vm.explain_decision(e, holding) if e else "",
                "見た数字": ", ".join(f"{k}={v}" for k, v in (e.get("logic") or {}).items() if k not in ("rule", "version")),
                "建玉": f"{held['通貨']} {held['売買']} {held['数量']:.0f}（{held['含み損益']:+,} 円）" if held else "なし",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with st.container(border=True):
        st.markdown("**最近の動き**")
        activity = [e for e in events if e.get("kind") in ("live_open", "live_close", "live_gone", "live_fatal",
                                                              "live_error", "live_skip")
                    or (e.get("kind") == "live_decision" and (e.get("decision") or {}).get("action") not in ("hold", None))][-10:]
        if not activity:
            st.caption("まだありません")
        labels = {"live_open": ("建てた", "ok"), "live_close": ("決済した", "info"), "live_gone": ("GMO側で決済", "info"),
                  "live_fatal": ("致命的な損失を防いだ", "bad"), "live_error": ("エラー", "bad"),
                  "live_skip": ("見送り", "warn"), "live_decision": ("判断", "info")}
        for e in reversed(activity):
            text, kind = labels[e["kind"]]
            detail = e.get("reason") or e.get("error") or e.get("note") or ""
            if e["kind"] == "live_open":
                detail = f"{vm.PAIR_LABELS.get(e.get('symbol'), e.get('symbol'))} {e.get('side')} {e.get('size')} @ {e.get('price')}"
            if e["kind"] == "live_decision":
                d = e.get("decision") or {}
                detail = f"{d.get('action')} {d.get('symbol', '')} → {'通過' if e.get('approved') else e.get('rejected')}"
            st.markdown(f"{pill(text, kind)} `{e['at'][5:16].replace('T', ' ')}` **{vm.account_label(e.get('account', ''))}** "
                        f"<span style='color:#64748b;font-size:.85rem'>{detail[:140]}</span>", unsafe_allow_html=True)


def page_trades() -> None:
    venue_header("取引履歴", "GMOコインの実際の約定（手数料・スワップ・ずれつき）", "ペーパー運用の仮想の約定")
    live(live_trades if venue() == "live" else paper_trades)()


def live_trades() -> None:
    events = live_log()
    owners = position_accounts(events)
    rows = [{"時刻": e["at"][5:16].replace("T", " "),
             "判断する者": vm.account_label(owners.get(int(e.get("positionId") or 0), "")) or "（手で建てた）",
             "通貨": vm.PAIR_LABELS.get(e.get("symbol"), e.get("symbol")), "売買": "買い" if e.get("side") == "BUY" else "売り",
             "新規/決済": "新規" if e.get("settleType") == "OPEN" else "決済", "数量": float(e.get("size") or 0),
             "値段": float(e.get("price") or 0), "損益": float(e.get("lossGain") or 0), "手数料": float(e.get("fee") or 0),
             "スワップ": float(e.get("settledSwap") or 0), "ずれ（pips）": e.get("slippage_pips")}
            for e in events if e.get("kind") == "execution"]
    if not rows:
        st.info("まだ約定はありません", icon=":material/hourglass_empty:")
        return
    st.dataframe(pd.DataFrame(list(reversed(rows))), hide_index=True, width="stretch",
                 column_config={"損益": st.column_config.NumberColumn(format="%+.0f 円")})


def paper_trades() -> None:
    events = fx_events()
    fills = [e for e in events if e.get("kind") in ("open", "close")]
    if not fills:
        st.info("まだ仮想の約定はありません", icon=":material/hourglass_empty:")
        return
    st.dataframe(pd.DataFrame([{
        "時刻": e.get("at", "")[5:16].replace("T", " "), "判断する者": vm.account_label(e.get("account", "")),
        "種類": "建てた" if e["kind"] == "open" else "閉じた", "通貨": vm.PAIR_LABELS.get(e.get("symbol"), e.get("symbol")),
        "数量": e.get("units"), "値段": round(e.get("price", e.get("exit_price", 0)) or 0, 3),
        "損益": round(e["pnl"]) if "pnl" in e else None, "理由": str(e.get("reason", ""))[:100],
    } for e in reversed(fills)][:300]), hide_index=True, width="stretch")


def page_decisions() -> None:
    venue_header("判断の記録", "実際の口座で、判断する者が何を見てどう判断したか", "ペーパーの口座での判断（AI と機械）")
    if venue() == "live":
        live(live_decisions)()
    else:
        live(paper_decisions)()


def live_decisions() -> None:
    events = [e for e in live_log() if e.get("kind") == "live_decision"]
    if not events:
        st.info("まだありません（見張り役が5分ごと・急変のときに記録します）", icon=":material/hourglass_empty:")
        return

    def source(e: dict) -> str:
        """この判断を、誰がどう出したか。"""
        if e.get("error"):
            return "🔴 AI を呼べなかった"
        if e.get("model"):
            return "🟢 AI" + ("（前回の判断を使い回し）" if e.get("reused") else "")
        if e.get("logic"):
            return "🟢 ロジック"
        return "⚪ AI を呼ばない回"

    rows = [(e, h) for e, h in zip(events, vm.holdings_over_time(events)) if e.get("kind") == "live_decision"]
    only = st.segmented_control("表示", ["すべて", "AI が実際に判断した回だけ", "売り買いした・しようとした回だけ"],
                                default="すべて", label_visibility="collapsed")
    if only == "AI が実際に判断した回だけ":
        rows = [(e, h) for e, h in rows if e.get("model")]
    elif only == "売り買いした・しようとした回だけ":
        rows = [(e, h) for e, h in rows if (e.get("decision") or {}).get("action") not in ("hold", None)]
    table = pd.DataFrame([{
        "時刻": e["at"][5:16].replace("T", " "), "判断する者": vm.account_label(e.get("account", "")),
        "判断": vm.action_label(e.get("decision") or {}, h),
        "理由": vm.explain_decision(e, h),
        "見た数字": ", ".join(f"{k}={v}" for k, v in (e.get("logic") or {}).items() if k not in ("rule", "version")),
        "出どころ": source(e),
        "通ったか": "" if (e.get("decision") or {}).get("action") in ("hold", None)
        else ("通った" if e.get("approved") else f"通らなかった: {e.get('rejected')}"),
        "リスク": f"資金の{float((e.get('decision') or {}).get('risk_fraction') or (e.get('logic') or {}).get('risk_fraction') or 0):.1%}"
        if str((e.get("decision") or {}).get("action", "")).startswith("open") else "",
        "自信": {"high": "高い", "medium": "中くらい", "low": "低い"}.get((e.get("decision") or {}).get("confidence"), ""),
        "気にしていること": (e.get("decision") or {}).get("risks", "") or "",
        "起こした理由": e.get("trigger", "") or "",
    } for e, h in reversed(rows)][:300])
    if table.empty:
        st.caption("該当する判断はありません")
    else:
        # 全部の行で空の列は出さない
        table = table.loc[:, [c for c in table.columns if table[c].astype(str).str.len().gt(0).any()]]
        st.dataframe(table, hide_index=True, width="stretch")
    with st.expander("ロジックの決まり（機械の判断）"):
        from jqbt.interfaces.fx_runtime import RULES, RULES_VERSION

        st.caption(f"版: {RULES_VERSION}")
        for key, text in RULES.items():
            st.markdown(f"- **{vm.account_label(key) if key != 'carry_trend' else '金利差＋流れ'}**: {text}")


def paper_decisions() -> None:
    fx_paper_body()


def page_compare() -> None:
    header("判断する者の比較", "同じ判断する者の、実運用とペーパーの成績を並べる")
    live(compare_body)()


def compare_body() -> None:
    from jqbt.infrastructure import fx_paper_log

    prices = quotes()
    paper = fx_paper_log.latest_snapshots(fx_events())
    events = live_log()
    closed = live_closed(events)
    open_rows = live_positions(events, prices)
    names = list(dict.fromkeys(list(paper) + [r["account"] for r in closed]))
    rows = []
    for name in names:
        state = paper.get(name)
        label = vm.account_label(name)
        live_pnl = sum(r["pnl"] for r in closed if r["account"] == name) + \
            sum(r["含み損益"] for r in open_rows if r["判断する者"] == label)
        rows.append({"判断する者": label, "種類": vm.account_kind(name),
                     "ペーパーの損益": round(marked_equity(state, prices) - state["initial"]) if state else None,
                     "実運用の損益": round(live_pnl) if name in LiveConfig().accounts else None,
                     "実運用の決済": sum(1 for r in closed if r["account"] == name)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "ペーパーの損益": st.column_config.NumberColumn(format="%+d 円"),
        "実運用の損益": st.column_config.NumberColumn(format="%+d 円")})
    st.caption("実運用はペーパーと数量（1口座1,000通貨まで）と約定の値段が違うので、損益の大きさは揃いません。"
               "実運用の損益は、決済した分の確定損益（手数料・スワップ込み）と、持っている建玉の含み損益の合計")


def page_controls() -> None:
    header("操作", "自動売買とペーパー運用の ON/OFF")
    show_flash()
    st.markdown(auto_trading_pills(), unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("**自動売買（実際のお金）**")
        fx_live_controls()
    with st.container(border=True):
        st.markdown("**ペーパー運用（シミュレーション）**")
        paper_switch()


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
        st.markdown("**値段の見張り役**")
        state = watcher_state()
        st.markdown(watcher_pill(), unsafe_allow_html=True)
        for w in reversed(state.get("wakes", [])[-10:]):
            st.caption(f"{w['at'][5:16].replace('T', ' ')} {w['reason']} → {w['result']}")
        if state and not state.get("wakes"):
            st.caption("まだ判断の回を起こしていません")
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
    "": [st.Page(page_overview, title="概要", icon=":material/dashboard:", default=True)],
    "取引": [st.Page(page_trades, title="取引履歴", icon=":material/receipt_long:", url_path="trades"),
           st.Page(page_decisions, title="判断の記録", icon=":material/psychology:", url_path="decisions"),
           st.Page(page_compare, title="判断する者の比較", icon=":material/compare_arrows:", url_path="compare"),
           st.Page(page_controls, title="操作", icon=":material/toggle_on:", url_path="controls")],
    "詳細": [st.Page(page_fx_live, title="口座の詳細（実運用）", icon=":material/account_balance_wallet:", url_path="fx-live"),
           st.Page(page_fx_paper, title="口座の詳細（ペーパー）", icon=":material/show_chart:", url_path="fx-paper"),
           st.Page(page_fx_lab, title="検証（ダミー）", icon=":material/science:", url_path="fx-lab")],
    **({"AI": ext_pages} if ext_pages else {}),
    "国内株": [st.Page(page_today, title="今日", icon=":material/today:", url_path="stocks"),
            st.Page(page_signals, title="売買候補と約定", icon=":material/assignment:", url_path="signals"),
            st.Page(page_settings, title="設定", icon=":material/tune:", url_path="settings"),
            st.Page(page_history, title="履歴", icon=":material/history:", url_path="history")],
    "システム": [st.Page(page_system, title="状態", icon=":material/info:", url_path="system")],
})

with st.sidebar:
    st.segmented_control("口座", list(VENUES), key="venue", default="実運用", width="stretch")
    st.segmented_control("画面の自動更新", list(REFRESH_CHOICES), key="refresh", default="30秒")
    if st.button("今すぐ更新", icon=":material/refresh:", width="stretch"):
        quotes.clear()
        st.rerun()
    with st.expander("国内株の設定"):
        st.selectbox("国内株の台帳", ["paper", "live"], key="profile",
                     format_func=lambda p: "ペーパー（仮想の約定）" if p == "paper" else "実運用（実際の約定を記録）")
    st.caption(DISCLAIMER)

navigation.run()
