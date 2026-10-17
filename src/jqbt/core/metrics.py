"""成績指標。

指標そのものより **その指標が何を隠すか** のほうが大事なので、
警告を一緒に返す設計にしている。数字だけ見て判断すると、
だいたい過学習した戦略を本番に出すことになる。

いちばん信用してよい数字は、勝率でも Sharpe でもなく

    1取引あたり期待値 と、その標準誤差（t値）

である。期待値 +0.1%、標準誤差 0.15% なら、優位性は「無い」と読むのが正しい。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

#: 年間営業日数（東証）
TRADING_DAYS = 245
#: 申告分離課税（所得税15% + 復興特別所得税 + 住民税5%）
TAX_RATE = 0.20315
#: これ未満の取引数では、どの指標も参考値として扱う
MIN_TRADES = 30


@dataclass
class Metrics:
    # --- 損益 ---
    initial_cash: float
    final_value: float
    total_return: float
    cagr: float | None
    #: 税引後（年間利益に20.315%）。損失年は課税されない前提の単純計算
    net_profit_after_tax: float

    # --- リスク ---
    max_drawdown: float
    max_drawdown_days: int
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    calmar: float | None

    # --- 取引 ---
    trade_count: int
    win_rate: float | None
    profit_factor: float | None
    average_win: float | None
    average_loss: float | None
    expectancy: float | None
    #: 期待値の標準誤差と t 値。優位性の有無はここで判断する
    expectancy_stderr: float | None
    t_statistic: float | None
    average_holding_days: float | None

    # --- コスト ---
    total_commission: float
    total_slippage: float
    cost_ratio: float | None

    # --- 稼働 ---
    average_exposure: float | None
    turnover: float | None

    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "warnings"}
        data["warnings"] = self.warnings
        return data


# ----------------------------------------------------------------------
def max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """最大ドローダウンと、その状態が続いた日数。

    率だけ見ると「-30% だが3日で回復」と「-30% で2年戻らない」が
    同じ数字になる。実際に耐えられるかは日数のほうが効く。
    """
    if equity.empty:
        return 0.0, 0
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    trough = drawdown.idxmin()
    depth = float(drawdown.min())

    # 谷の直前のピークから、そのピークを回復するまでの日数
    peak_value = float(peak.loc[trough])
    before = equity.loc[:trough]
    peak_date = before[before >= peak_value].index
    start = peak_date[-1] if len(peak_date) else equity.index[0]
    after = equity.loc[trough:]
    recovered = after[after >= peak_value]
    end = recovered.index[0] if len(recovered) else equity.index[-1]
    return depth, int((end - start).days)


def _pearson_safe(values: np.ndarray) -> float | None:
    return None if values.size == 0 else float(values.mean())


def compute(
    result,  # BacktestResult
    risk_free_rate: float = 0.0,
    benchmark: pd.Series | None = None,
) -> Metrics:
    """バックテスト結果から指標一式を出す。"""
    if not result.snapshots:
        raise ValueError("資産曲線が空です。対象期間にデータがありません")
    equity = pd.Series(
        [s.equity for s in result.snapshots],
        index=pd.DatetimeIndex([s.date for s in result.snapshots]),
        dtype=float,
        name="equity",
    )
    warnings: list[str] = []

    initial = float(result.initial_cash)
    final = float(equity.iloc[-1])
    total_return = final / initial - 1.0

    days = (equity.index[-1] - equity.index[0]).days
    years = days / 365.25
    cagr = ((final / initial) ** (1 / years) - 1.0) if years >= 0.5 and final > 0 else None
    if years < 1.0:
        warnings.append(
            f"検証期間が{years:.1f}年しかありません。CAGR も最大DDも期間に強く依存します"
        )

    daily = equity.pct_change().dropna()
    volatility = float(daily.std() * math.sqrt(TRADING_DAYS)) if len(daily) > 1 else None

    sharpe = None
    if len(daily) > 1 and daily.std() > 0:
        excess = daily - risk_free_rate / TRADING_DAYS
        sharpe = float(excess.mean() / daily.std() * math.sqrt(TRADING_DAYS))
        autocorr = float(daily.autocorr(lag=1)) if len(daily) > 3 else 0.0
        if abs(autocorr) > 0.1:
            warnings.append(
                f"日次リターンに自己相関があります（lag1={autocorr:+.2f}）。"
                "√245 での年率換算は iid を仮定しているため、Sharpe が実態とずれます"
            )

    downside = daily[daily < 0]
    sortino = (
        float(daily.mean() / downside.std() * math.sqrt(TRADING_DAYS))
        if len(downside) > 1 and downside.std() > 0
        else None
    )

    depth, dd_days = max_drawdown(equity)
    calmar = (cagr / abs(depth)) if cagr is not None and depth < 0 else None

    # --- 取引単位の分析 ---
    trips = result.round_trips
    returns = np.array([t.return_pct for t in trips], dtype=float)
    pnls = np.array([t.pnl for t in trips], dtype=float)

    win_rate = profit_factor = average_win = average_loss = None
    expectancy = stderr = t_stat = average_holding = None

    if len(trips):
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        win_rate = float(len(wins) / len(pnls))
        gross_profit = float(wins.sum())
        gross_loss = float(-losses.sum())
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
        average_win = float(wins.mean()) if len(wins) else None
        average_loss = float(losses.mean()) if len(losses) else None
        expectancy = float(returns.mean())
        if len(returns) > 1:
            stderr = float(returns.std(ddof=1) / math.sqrt(len(returns)))
            t_stat = (expectancy / stderr) if stderr > 0 else None
        average_holding = float(np.mean([t.holding_days for t in trips]))

    if len(trips) < MIN_TRADES:
        warnings.append(
            f"取引数が{len(trips)}件しかありません（目安{MIN_TRADES}件以上）。"
            "勝率・Profit Factor はこの件数では運の範囲を出ません"
        )
    if t_stat is not None and abs(t_stat) < 2.0:
        warnings.append(
            f"1取引あたり期待値の t値が {t_stat:+.2f} です。"
            "|t| < 2 は「優位性があるとは言えない」と読むべきです"
        )
    if profit_factor is not None and win_rate is not None:
        if win_rate > 0.7 and profit_factor < 1.5:
            warnings.append(
                "勝率は高いが Profit Factor が低い形です。"
                "小さく勝って大きく負ける（損切りが遅い）パターンに該当しないか確認してください"
            )

    # --- コスト ---
    commission = float(sum(f.commission for f in result.fills))
    slippage = float(sum(f.slippage for f in result.fills))
    gross = final - initial + commission + slippage
    cost_ratio = ((commission + slippage) / abs(gross)) if gross else None
    if cost_ratio is not None and cost_ratio > 0.3:
        warnings.append(
            f"粗利の{cost_ratio * 100:.0f}%がコストで消えています。"
            "コスト前提を少し変えるだけで結論が反転する水準です"
        )

    # --- 稼働 ---
    exposure = (
        float(sum(s.exposure for s in result.snapshots) / len(result.snapshots))
        if result.snapshots
        else None
    )
    traded = float(sum(f.notional for f in result.fills))
    turnover = (traded / initial / years) if years > 0 else None

    # --- 税引後 ---
    profit = final - initial
    net_after_tax = profit * (1 - TAX_RATE) if profit > 0 else profit

    if benchmark is not None and len(benchmark) > 1:
        bench_return = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1.0)
        if total_return <= bench_return:
            warnings.append(
                f"ベンチマーク（{bench_return * 100:+.1f}%）を上回っていません"
                f"（戦略 {total_return * 100:+.1f}%）。買って持つだけのほうが良い結果です"
            )

    if result.rejections:
        rate = 1.0 - (result.fill_rate or 1.0)
        if rate > 0.1:
            warnings.append(
                f"注文の{rate * 100:.0f}%が約定しませんでした。"
                "流動性の仮定が厳しいか、対象銘柄が薄すぎます"
            )

    return Metrics(
        initial_cash=initial,
        final_value=final,
        total_return=total_return,
        cagr=cagr,
        net_profit_after_tax=net_after_tax,
        max_drawdown=depth,
        max_drawdown_days=dd_days,
        volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        trade_count=len(trips),
        win_rate=win_rate,
        profit_factor=profit_factor,
        average_win=average_win,
        average_loss=average_loss,
        expectancy=expectancy,
        expectancy_stderr=stderr,
        t_statistic=t_stat,
        average_holding_days=average_holding,
        total_commission=commission,
        total_slippage=slippage,
        cost_ratio=cost_ratio,
        average_exposure=exposure,
        turnover=turnover,
        warnings=warnings,
    )


# ----------------------------------------------------------------------
def format_report(metrics: Metrics, title: str = "") -> str:
    """人間が読む形。警告を最後にまとめて出す。"""

    def pct(value: float | None, digits: int = 2) -> str:
        return "—" if value is None else f"{value * 100:.{digits}f}%"

    def num(value: float | None, digits: int = 2) -> str:
        return "—" if value is None else f"{value:.{digits}f}"

    def yen(value: float | None) -> str:
        return "—" if value is None else f"{value:,.0f}円"

    lines = []
    if title:
        lines += [title, "=" * len(title)]
    lines += [
        "",
        "【損益】",
        f"  初期資金            {yen(metrics.initial_cash)}",
        f"  最終評価額          {yen(metrics.final_value)}",
        f"  総リターン          {pct(metrics.total_return)}",
        f"  CAGR                {pct(metrics.cagr)}",
        f"  税引後利益          {yen(metrics.net_profit_after_tax)}",
        "",
        "【リスク】",
        f"  最大ドローダウン    {pct(metrics.max_drawdown)}  （{metrics.max_drawdown_days}日）",
        f"  年率ボラティリティ  {pct(metrics.volatility)}",
        f"  Sharpe              {num(metrics.sharpe)}",
        f"  Sortino             {num(metrics.sortino)}",
        f"  Calmar              {num(metrics.calmar)}",
        "",
        "【取引】",
        f"  取引数（往復）      {metrics.trade_count}",
        f"  勝率                {pct(metrics.win_rate, 1)}",
        f"  Profit Factor       {num(metrics.profit_factor)}",
        f"  平均利益 / 平均損失 {yen(metrics.average_win)} / {yen(metrics.average_loss)}",
        f"  1取引期待値         {pct(metrics.expectancy)}"
        f"  ± {pct(metrics.expectancy_stderr)}",
        f"  t値                 {num(metrics.t_statistic)}"
        f"   {'← 優位性あり' if (metrics.t_statistic or 0) > 2 else '← 有意とは言えない'}",
        f"  平均保有日数        {num(metrics.average_holding_days, 1)}",
        "",
        "【コスト】",
        f"  手数料              {yen(metrics.total_commission)}",
        f"  スリッページ        {yen(metrics.total_slippage)}",
        f"  粗利に対する比率    {pct(metrics.cost_ratio, 1)}",
        "",
        "【稼働】",
        f"  平均投資比率        {pct(metrics.average_exposure, 1)}",
        f"  年間回転率          {num(metrics.turnover)}回",
    ]

    if metrics.warnings:
        lines += ["", "【警告】"]
        lines += [f"  ! {w}" for w in metrics.warnings]
    return "\n".join(lines)
