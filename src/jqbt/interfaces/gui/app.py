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
from jqbt.interfaces import viewmodels as vm
from jqbt.interfaces.composition import DataRequest, load_panel
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
st.set_page_config(page_title="運用の操作盤", layout="wide")


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
        return "unknown", False


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
    st.altair_chart(chart, use_container_width=True)


def cards(items: list[dict[str, str]], per_row: int = 2) -> None:
    """数字のカード。幅が狭いと値が切れるので、2列ずつ並べる。"""
    for start in range(0, len(items), per_row):
        row = items[start : start + per_row]
        for column, item in zip(st.columns(per_row), row):
            column.metric(item["label"], item["value"], item.get("note") or None, delta_color="off")


# ----------------------------------------------------------------------
# サイドバー
# ----------------------------------------------------------------------
st.sidebar.title("運用の操作盤")
profile = st.sidebar.radio(
    "プロファイル", ["paper", "live"],
    format_func=lambda p: "ペーパー（仮想の約定）" if p == "paper" else "実運用（実際の約定を記録）",
)
PAGES = ["今日", "売買候補と約定", "設定", "履歴", "FX検証（ダミーデータ）"]
# タブは描き直すと先頭に戻るので、選んだ画面が保たれるサイドバーで切り替える
page = st.sidebar.radio("画面", PAGES, key="page")
try:
    panel, index = market()
    latest = index.dates[-1]
except Exception as exc:  # データが無いときも画面は出す
    st.error(f"相場データを読み込めません: {exc}。先に `make fetch` でデータを取得してください。")
    st.stop()

fresh = vm.freshness(latest, dt.date.today())
st.sidebar.caption("相場データ")
(st.sidebar.success if fresh["ok"] else st.sidebar.warning)(fresh["message"])
st.sidebar.caption(f"J-Quants の APIキー: {vm.secret_state(os.environ.get('JQUANTS_API_KEY'))}")
if kill_path(profile).exists():
    st.sidebar.error("キルスイッチが入っています")
st.sidebar.divider()
st.sidebar.caption(DISCLAIMER)

ops = service(profile)
ops.apply_pending(dt.datetime.now())

if "flash" in st.session_state:
    kind, message = st.session_state.pop("flash")
    getattr(st, kind)(message)

st.title(page)

# ----------------------------------------------------------------------
# 今日
# ----------------------------------------------------------------------
if page == "今日":
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
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.caption("保有はありません")
            st.caption(f"戦略 {status['strategy']} {status['params']} / 凍結した版 {status['version']} / 最後の入れ替え {status['last_rebalance'] or '—'}")
        with right:
            st.subheader("増額の条件（全部満たすまで資金を増やさない）")
            st.dataframe(pd.DataFrame(vm.gate_rows(status["gates"])), hide_index=True, use_container_width=True)
        if status["planned"]:
            st.info(f"未約定の候補が {len(status['planned'])} 件あります（「売買候補と約定」で記録）")

# ----------------------------------------------------------------------
# 売買候補と約定
# ----------------------------------------------------------------------
if page == "売買候補と約定":
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
                        st.dataframe(pd.DataFrame(vm.signal_rows(result.signals)), hide_index=True, use_container_width=True)
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

# ----------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------
if page == "設定":
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
            st.dataframe(pd.DataFrame(change["rows"]), hide_index=True, use_container_width=True)
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

# ----------------------------------------------------------------------
# 履歴
# ----------------------------------------------------------------------
if page == "履歴":
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
            st.dataframe(pd.DataFrame(fills), hide_index=True, use_container_width=True)
        else:
            st.caption("まだありません")
        st.subheader("売買候補")
        signals = vm.signal_rows(ops.ledger.signals())
        if signals:
            st.dataframe(pd.DataFrame(signals), hide_index=True, use_container_width=True)
        else:
            st.caption("まだありません")
        st.subheader("設定変更の履歴（監査ログ）")
        st.dataframe(pd.DataFrame(vm.audit_rows(ops.ledger.audit_log(200))), hide_index=True, use_container_width=True)

# ----------------------------------------------------------------------
# FX検証（ダミーデータ）
# ----------------------------------------------------------------------
if page == "FX検証（ダミーデータ）":
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
            st.dataframe(trades.tail(200), hide_index=True, use_container_width=True)
