"""過学習の診断（純粋関数）。

ウォークフォワードを回すと「学習期間の成績」と「検証期間の成績」が
窓の数だけ手に入る。その **差** と **ばらつき** を見るのがここの仕事。

見るのは3つ。

  1. **劣化率** — 検証が学習よりどれだけ悪いか。
     良い戦略でも多少は劣化する（学習期間で最良を選んでいるため）。
     半分くらいまでは正常。7割以上落ちるなら過学習を強く疑う

  2. **パラメータの安定性** — 窓ごとに選ばれたパラメータがばらついていないか。
     25/75 → 5/200 → 40/60 のように毎回変わるなら、その「最適値」は
     ノイズを拾っているだけ。実運用で次にどれを使えばいいかも決められない

  3. **検証期間の一貫性** — 検証成績がプラスの窓がいくつあるか。
     全体でプラスでも、1つの窓の大勝ちが全部を作っているなら再現性はない
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """総合判定。甘く出さない。"""

    INSUFFICIENT = "insufficient"  # データ不足で判定不能
    OVERFIT = "overfit"            # 過学習が疑わしい
    FRAGILE = "fragile"            # 条件次第で壊れる
    PLAUSIBLE = "plausible"        # 否定はされていない（「有効」ではない）

    @property
    def label(self) -> str:
        return {
            Verdict.INSUFFICIENT: "判定不能（データ不足）",
            Verdict.OVERFIT: "過学習が疑わしい",
            Verdict.FRAGILE: "不安定（条件次第で壊れる）",
            Verdict.PLAUSIBLE: "否定はされていない",
        }[self]


@dataclass(frozen=True, slots=True)
class ParameterStability:
    """1つのパラメータが窓ごとにどれだけ動いたか。"""

    name: str
    values: tuple[Any, ...]
    distinct: int
    #: 数値パラメータのみ。変動係数（標準偏差 ÷ 平均）
    coefficient_of_variation: float | None

    @property
    def is_stable(self) -> bool:
        """毎回ほぼ同じものが選ばれているか。"""
        if self.coefficient_of_variation is not None:
            return self.coefficient_of_variation < 0.3
        return self.distinct <= max(1, len(self.values) // 3)


@dataclass(frozen=True)
class WalkForwardDiagnosis:
    windows: int
    #: 学習期間の平均スコア / 検証期間の平均スコア
    mean_train_score: float | None
    mean_test_score: float | None
    #: 劣化率。1.0 なら全部消えた、0.0 なら劣化なし
    degradation: float | None
    #: 検証成績がプラスだった窓の割合
    positive_window_ratio: float | None
    parameter_stability: tuple[ParameterStability, ...] = ()
    verdict: Verdict = Verdict.INSUFFICIENT
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


# ----------------------------------------------------------------------
#: 窓がこれ未満なら判定しない
MIN_WINDOWS = 5
#: これを超える劣化は過学習を疑う
OVERFIT_DEGRADATION = 0.7
#: 検証がプラスの窓がこれ未満なら不安定
MIN_POSITIVE_RATIO = 0.5


def degradation_ratio(train: float | None, test: float | None) -> float | None:
    """学習から検証への劣化率。

    符号の扱いに注意が必要。学習がマイナスなら比率に意味がないので None を返す。
    「マイナスがもっとマイナスになった」を 0.5 のような数字にすると誤読する。
    """
    if train is None or test is None:
        return None
    if train <= 0:
        return None
    return max(0.0, min(1.0, (train - test) / train))


def parameter_stability(
    selections: list[dict[str, Any]]
) -> tuple[ParameterStability, ...]:
    """窓ごとに選ばれたパラメータのばらつき。"""
    if not selections:
        return ()

    names = sorted({k for s in selections for k in s})
    result = []
    for name in names:
        values = tuple(s.get(name) for s in selections)
        numeric = [float(v) for v in values if isinstance(v, (int, float))]
        cv = None
        if len(numeric) == len(values) and len(numeric) > 1:
            mean = statistics.fmean(numeric)
            if abs(mean) > 1e-12:
                cv = statistics.pstdev(numeric) / abs(mean)
        result.append(
            ParameterStability(
                name=name,
                values=values,
                distinct=len({str(v) for v in values}),
                coefficient_of_variation=cv,
            )
        )
    return tuple(result)


def diagnose(
    train_scores: list[float | None],
    test_scores: list[float | None],
    selections: list[dict[str, Any]],
) -> WalkForwardDiagnosis:
    """ウォークフォワードの結果から過学習の兆候を読む。

    **「有効」とは決して判定しない。** 出せるのは
    「否定はされていない」までで、それが統計でできる限界。
    """
    windows = len(test_scores)
    usable_train = [s for s in train_scores if s is not None]
    usable_test = [s for s in test_scores if s is not None]

    mean_train = statistics.fmean(usable_train) if usable_train else None
    mean_test = statistics.fmean(usable_test) if usable_test else None
    degradation = degradation_ratio(mean_train, mean_test)
    positive_ratio = (
        sum(1 for s in usable_test if s > 0) / len(usable_test) if usable_test else None
    )
    stability = parameter_stability(selections)

    warnings: list[str] = []
    notes: list[str] = []

    if windows < MIN_WINDOWS:
        warnings.append(
            f"窓が{windows}個しかありません（目安{MIN_WINDOWS}個以上）。"
            "この数では、結果は偶然に支配されます"
        )
        verdict = Verdict.INSUFFICIENT
    else:
        verdict = Verdict.PLAUSIBLE

        if degradation is not None and degradation >= OVERFIT_DEGRADATION:
            warnings.append(
                f"学習から検証への劣化が{degradation * 100:.0f}%です。"
                "学習期間に合わせ込んでいる可能性が高いです"
            )
            verdict = Verdict.OVERFIT
        elif degradation is not None and degradation > 0.5:
            notes.append(
                f"劣化{degradation * 100:.0f}%。選択バイアスの範囲内ですが、"
                "実運用の期待値は検証側の数字で見てください"
            )

        if mean_test is not None and mean_test <= 0:
            warnings.append(
                f"検証期間の平均スコアが {mean_test:.2f} です。"
                "未知の期間では優位性が残っていません"
            )
            verdict = Verdict.OVERFIT

        if positive_ratio is not None and positive_ratio < MIN_POSITIVE_RATIO:
            warnings.append(
                f"検証でプラスだった窓が{positive_ratio * 100:.0f}%しかありません。"
                "一部の期間の大勝ちが全体を作っていないか確認してください"
            )
            if verdict is Verdict.PLAUSIBLE:
                verdict = Verdict.FRAGILE

        unstable = [p for p in stability if not p.is_stable]
        if unstable:
            names = "、".join(p.name for p in unstable)
            warnings.append(
                f"窓ごとに選ばれる値が安定していません（{names}）。"
                "その「最適値」はノイズを拾っている可能性があります。"
                "実運用でどの値を使うかも決められません"
            )
            if verdict is Verdict.PLAUSIBLE:
                verdict = Verdict.FRAGILE

    if verdict is Verdict.PLAUSIBLE:
        notes.append(
            "統計的に否定はされませんでした。ただしこれは「有効」という意味ではなく、"
            "「このデータでは棄却できなかった」だけです"
        )

    return WalkForwardDiagnosis(
        windows=windows,
        mean_train_score=mean_train,
        mean_test_score=mean_test,
        degradation=degradation,
        positive_window_ratio=positive_ratio,
        parameter_stability=stability,
        verdict=verdict,
        warnings=tuple(warnings),
        notes=tuple(notes),
    )
