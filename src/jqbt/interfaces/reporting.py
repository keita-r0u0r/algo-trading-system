"""結果の整形。表示だけを受け持ち、計算はしない。"""

from __future__ import annotations

from ..application.walkforward import WalkForwardReport
from ..core.diagnostics import Verdict
from ..core.splitting import MIN_RELIABLE_WINDOWS, Window


def pct(value: float | None, digits: int = 2, sign: bool = False) -> str:
    if value is None:
        return "—"
    fmt = f"{{:+.{digits}f}}%" if sign else f"{{:.{digits}f}}%"
    return fmt.format(value * 100)


def num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def yen(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}円"


# ----------------------------------------------------------------------
def format_windows(windows: tuple[Window, ...]) -> str:
    lines = ["【分割】"]
    for window in windows:
        lines.append(f"  {window.label()}")
    if len(windows) < MIN_RELIABLE_WINDOWS:
        lines.append(
            f"  ! 窓が{len(windows)}個しかありません（目安{MIN_RELIABLE_WINDOWS}個以上）。"
        )
        lines.append("    この数では、結果は偶然に支配されます。期間を延ばしてください")
    return "\n".join(lines)


def format_walkforward(report: WalkForwardReport) -> str:
    objective = report.objective_name
    lines = [
        "",
        "=" * 72,
        f"ウォークフォワード検証（目的関数: {objective}）",
        "=" * 72,
        "",
        f"{'窓':<4}{'選ばれたパラメータ':<26}{'学習':>9}{'検証':>9}{'検証リターン':>14}{'取引':>7}",
        "-" * 72,
    ]

    for outcome in report.outcomes:
        if outcome.error:
            lines.append(f"#{outcome.window.index:<3}{outcome.error}")
            continue
        params = ", ".join(f"{k}={v}" for k, v in sorted(outcome.params.items())) or "—"
        train = outcome.train_metrics
        test = outcome.test_metrics
        train_score = _score(report, train)
        test_score = _score(report, test)
        lines.append(
            f"#{outcome.window.index:<3}"
            f"{params:<26}"
            f"{num(train_score):>9}"
            f"{num(test_score):>9}"
            f"{pct(test.total_return if test else None, sign=True):>14}"
            f"{(test.trade_count if test else 0):>7}"
        )

    diagnosis = report.diagnosis
    lines += [
        "",
        "【診断】",
        f"  判定              {diagnosis.verdict.label}",
        f"  窓の数            {diagnosis.windows}",
        f"  学習の平均スコア  {num(diagnosis.mean_train_score)}",
        f"  検証の平均スコア  {num(diagnosis.mean_test_score)}",
        f"  劣化率            {pct(diagnosis.degradation, 0)}"
        + ("   ← 学習側がマイナスのため計算不能" if diagnosis.degradation is None
           and (diagnosis.mean_train_score or 0) <= 0 else ""),
        f"  検証がプラスの窓  {pct(diagnosis.positive_window_ratio, 0)}",
    ]

    if diagnosis.parameter_stability:
        lines += ["", "【パラメータの安定性】"]
        for stability in diagnosis.parameter_stability:
            mark = "安定" if stability.is_stable else "不安定"
            values = ", ".join(str(v) for v in stability.values)
            cv = (
                f" 変動係数{stability.coefficient_of_variation:.2f}"
                if stability.coefficient_of_variation is not None
                else ""
            )
            lines.append(f"  {stability.name:<14}{mark}  [{values}]{cv}")

    # 検証期間を繋いだ成績
    if report.chained_equity is not None and len(report.chained_equity) > 1:
        curve = report.chained_equity
        total = float(curve.iloc[-1] / curve.iloc[0] - 1.0)
        peak = curve.cummax()
        drawdown = float((curve / peak - 1.0).min())
        lines += [
            "",
            "【検証期間だけを繋いだ成績（報告してよいのはこれ）】",
            f"  期間              {curve.index[0].date()} 〜 {curve.index[-1].date()}",
            f"  総リターン        {pct(total, sign=True)}",
            f"  最大ドローダウン  {pct(drawdown)}",
        ]

    if diagnosis.warnings:
        lines += ["", "【警告】"]
        lines += [f"  ! {w}" for w in diagnosis.warnings]
    if diagnosis.notes:
        lines += ["", "【補足】"]
        lines += [f"  - {n}" for n in diagnosis.notes]
    if report.notes:
        lines += [f"  - {n}" for n in report.notes]

    # 選択バイアスの注記
    selections = [o.selection for o in report.outcomes if o.selection]
    if selections:
        failed_null = [s for s in selections if s.beats_null is False]
        attempted = selections[0].attempted
        compared = [s.trials for s in selections]
        lines += ["", "【選択バイアス】"]
        lines.append(f"  各窓の格子        {attempted} 通り")
        lines.append(
            f"  実際に比較できた数 {min(compared)}〜{max(compared)} 通り"
            "（取引数が足りない候補は除外）"
        )
        if failed_null:
            lines.append(
                f"  ! {len(failed_null)}/{len(selections)} の窓で、"
                "「優位性ゼロでも期待される水準」を超えていません"
            )
            lines.append("    その窓で選ばれたパラメータに意味があるとは言えません")

    if diagnosis.verdict is Verdict.PLAUSIBLE:
        lines += [
            "",
            "※「否定はされていない」は「有効」ではありません。",
            "  このデータでは棄却できなかった、というだけです。",
        ]

    return "\n".join(lines)


def _score(report: WalkForwardReport, metrics) -> float | None:  # noqa: ANN001
    if metrics is None:
        return None
    from ..core.selection import OBJECTIVES

    return OBJECTIVES[report.objective_name](metrics)
