"""パラメータ選択と、多重検定の補正（純粋関数）。

## ここが検証で最も嘘をつきやすい場所

パラメータを50通り試して一番良いものを選ぶと、その成績には
**「50回引いた中の最大値」という偏り**が必ず乗る。真の優位性がゼロでも、
50回引けば見栄えのする1つは出る。

対策は2つある。

  1. 選んだパラメータを **未知の期間** で評価する（ウォークフォワード）
  2. 「何もない場合に、N回試したら最大どのくらい良く見えるか」を
     計算して、自分の結果と比べる（このファイル）

2 は Bailey & López de Prado の考え方に基づく。真の Sharpe が 0 の
戦略を N 個試したときに観測される最大 Sharpe の期待値は、独立性を仮定して

    E[max SR] ≈ (1-γ)·Z⁻¹(1 - 1/N) + γ·Z⁻¹(1 - 1/(N·e))

（γ はオイラー・マスケローニ定数 ≈ 0.5772、Z⁻¹ は標準正規の逆累積分布）

この値を超えていなければ、**その戦略は「何もない」と区別がつかない。**

## 何を最大化するか

既定は Sharpe。総リターンを最大化すると、最もボラティリティの高い
パラメータが選ばれる（大きく賭ければリターンの期待最大値は上がる）。
「期待値 ÷ ばらつき」で選ばないと、運の要素を最大化してしまう。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Callable

#: オイラー・マスケローニ定数
EULER_GAMMA = 0.5772156649015329

_NORMAL = NormalDist()


# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Candidate:
    """試したパラメータ1組と、その成績。"""

    params: dict[str, Any]
    score: float
    #: 参考情報（取引数など）。選択には使わないが記録に残す
    detail: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(self.params.items()))


@dataclass(frozen=True, slots=True)
class Selection:
    """選ばれたパラメータと、選択に伴うバイアスの評価。"""

    best: Candidate
    #: 実際に比較した候補の数。多重検定の補正はこちらで行う
    trials: int
    #: 格子を展開した総数。取引数が足りず除外されたものを含む
    attempted: int
    #: 真の優位性がゼロの場合に期待される「最良スコア」
    expected_best_under_null: float | None
    #: 上の値を超えているか。超えていなければ選択バイアスと区別がつかない
    beats_null: bool | None
    #: 候補のスコアのばらつき。小さいほど「どれを選んでも同じ」
    score_spread: float
    note: str

    @property
    def params(self) -> dict[str, Any]:
        return self.best.params


# ----------------------------------------------------------------------
def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """パラメータの格子をすべての組み合わせに展開する。

    組み合わせ数がそのまま「試行回数 N」になり、選択バイアスの大きさを決める。
    気軽に軸を増やすと N は掛け算で増える。3軸×5値で125通り。
    """
    if not grid:
        return [{}]
    keys = sorted(grid)
    return [
        dict(zip(keys, values))
        for values in itertools.product(*(grid[k] for k in keys))
    ]


def expected_max_sharpe(trials: int, sample_size: int) -> float | None:
    """真の Sharpe が0の戦略を ``trials`` 回試したときの、最大 Sharpe の期待値。

    ``sample_size`` は Sharpe の推定に使った標本数（取引数、または観測日数）。
    推定量の標準誤差が 1/√n で効くため、標本が少ないほど「まぐれ」が大きく出る。

    独立試行を仮定しているので、パラメータが似通っていて相関が高い場合は
    やや保守的（＝厳しすぎる）側に出る。それでも、何の補正もしないよりは
    はるかに実態に近い。
    """
    if trials < 1 or sample_size < 2:
        return None
    if trials == 1:
        return 0.0

    n = float(trials)
    quantile = (1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / n) + EULER_GAMMA * _NORMAL.inv_cdf(
        1 - 1 / (n * math.e)
    )
    # Sharpe 推定量の標準誤差（正規性を仮定した近似）
    standard_error = 1.0 / math.sqrt(sample_size)
    return quantile * standard_error


def deflated_sharpe(
    observed: float, trials: int, sample_size: int
) -> float | None:
    """多重検定を差し引いた後の Sharpe（概算）。

    「N回試した中の最良」というゲタを脱がせた値。
    0以下なら、その結果は選択バイアスで説明がつく。
    """
    expected = expected_max_sharpe(trials, sample_size)
    if expected is None:
        return None
    return observed - expected


def select_best(
    candidates: list[Candidate],
    sample_size: int,
    higher_is_better: bool = True,
    attempted: int | None = None,
) -> Selection:
    """最良の候補を選び、その選択がどれだけ疑わしいかを添えて返す。

    **スコアだけを返さない。** 「何通り試したか」を失うと、
    あとから多重検定の補正ができなくなるため。
    """
    if not candidates:
        raise ValueError("候補が空です")

    ordered = sorted(candidates, key=lambda c: c.score, reverse=higher_is_better)
    best = ordered[0]
    scores = [c.score for c in candidates]
    spread = max(scores) - min(scores)

    expected = expected_max_sharpe(len(candidates), sample_size)
    beats = None if expected is None else best.score > expected

    attempted = attempted if attempted is not None else len(candidates)
    filtered = attempted - len(candidates)

    if len(candidates) == 1:
        note = (
            "比較できた候補が1つなので選択バイアスはありません"
            + (f"（{filtered}通りは取引数不足で除外）" if filtered else "")
        )
    elif expected is None:
        note = "標本が少なすぎて、選択バイアスの大きさを評価できません"
    elif not beats:
        note = (
            f"{len(candidates)}通り試した中の最良ですが、"
            f"優位性がゼロでも期待される水準（{expected:.2f}）を超えていません。"
            "選んだパラメータに意味があるとは言えません"
        )
    elif spread < 1e-9:
        note = "すべての候補が同じスコアです。パラメータが結果に効いていません"
    else:
        note = (
            f"{len(candidates)}通り試した中の最良です。"
            f"偶然の期待値（{expected:.2f}）は超えていますが、"
            "未知の期間で再現するかは検証期間で確かめてください"
        )

    if filtered and len(candidates) > 1:
        note += f"（格子{attempted}通りのうち{filtered}通りは取引数不足で除外）"

    return Selection(
        best=best,
        trials=len(candidates),
        attempted=attempted,
        expected_best_under_null=expected,
        beats_null=beats,
        score_spread=spread,
        note=note,
    )


# ----------------------------------------------------------------------
#: 目的関数。指標から1つの数値を取り出す
Objective = Callable[[Any], float | None]


def sharpe_objective(metrics) -> float | None:  # noqa: ANN001 - core.metrics.Metrics
    """既定の目的関数。リスクあたりのリターン。"""
    return metrics.sharpe


def expectancy_t_objective(metrics) -> float | None:  # noqa: ANN001
    """1取引あたり期待値の t値。

    取引数が少ない戦略に厳しく出るので、回転の低い戦略同士を
    比べるときはこちらのほうが素直。
    """
    return metrics.t_statistic


def total_return_objective(metrics) -> float | None:  # noqa: ANN001
    """総リターン。**既定にしてはいけない。**

    最もボラティリティの高いパラメータが選ばれる。大きく賭ければ
    リターンの期待最大値は上がるので、運の要素を最大化することになる。
    比較のために用意してあるだけ。
    """
    return metrics.total_return


OBJECTIVES: dict[str, Objective] = {
    "sharpe": sharpe_objective,
    "t_stat": expectancy_t_objective,
    "total_return": total_return_objective,
}
