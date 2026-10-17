"""期間分割・パラメータ選択・過学習診断（core の純粋関数）。

DBもファイルも使わないので一瞬で走る。FCIS を採ったことの見返り。

ここで固定したいのは主に2つ。

  1. 学習期間と検証期間が **絶対に重ならない** こと
  2. 「N通り試した中の最良」というゲタを、正しく差し引けること
"""

from __future__ import annotations

import datetime as dt

import pytest

from jqbt.core.diagnostics import (
    Verdict,
    degradation_ratio,
    diagnose,
    parameter_stability,
)
from jqbt.core.selection import (
    Candidate,
    expand_grid,
    expected_max_sharpe,
    select_best,
)
from jqbt.core.splitting import (
    Window,
    coverage,
    holdout,
    validate_no_overlap,
    walk_forward,
)


def business_days(count: int, start: dt.date = dt.date(2020, 1, 1)) -> list[dt.date]:
    days, cursor = [], start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


# ----------------------------------------------------------------------
# 期間分割
# ----------------------------------------------------------------------
def test_window_rejects_overlapping_periods():
    """学習と検証が重なる窓は作れないこと。

    重なった時点で検証の意味が消えるので、値オブジェクトの段階で弾く。
    """
    with pytest.raises(ValueError, match="重なっています"):
        Window(
            index=0,
            train_start=dt.date(2020, 1, 1),
            train_end=dt.date(2020, 6, 30),
            test_start=dt.date(2020, 6, 30),  # 学習の最終日と同じ
            test_end=dt.date(2020, 12, 31),
        )


def test_holdout_splits_without_gap_or_overlap():
    days = business_days(100)
    window = holdout(days, train_ratio=0.7)

    assert window.train_start == days[0]
    assert window.test_end == days[-1]
    assert window.train_end < window.test_start
    #: 境界に穴がない（学習の次の営業日が検証の初日）
    assert days[days.index(window.train_end) + 1] == window.test_start


def test_walk_forward_windows_do_not_overlap():
    """検証期間が重なっていないこと。

    重なると同じ日を複数回成績に数えることになり、成績が水増しされる。
    """
    windows = walk_forward(business_days(500), train_size=100, test_size=50)

    assert len(windows) > 1
    validate_no_overlap(windows)  # 例外が出なければ合格
    for window in windows:
        assert window.train_end < window.test_start


def test_walk_forward_rolling_keeps_train_width_constant():
    days = business_days(400)
    windows = walk_forward(days, train_size=100, test_size=50, anchored=False)

    starts = {w.train_start for w in windows}
    assert len(starts) == len(windows), "rolling なのに開始点が固定されています"


def test_walk_forward_anchored_keeps_start_fixed():
    days = business_days(400)
    windows = walk_forward(days, train_size=100, test_size=50, anchored=True)

    assert len({w.train_start for w in windows}) == 1
    #: 学習期間が窓ごとに伸びていく
    widths = [w.train_days for w in windows]
    assert widths == sorted(widths)
    assert widths[-1] > widths[0]


def test_walk_forward_returns_nothing_when_period_is_too_short():
    windows = walk_forward(business_days(80), train_size=100, test_size=50)
    assert windows == ()


def test_coverage_reports_only_the_test_span():
    days = business_days(400)
    windows = walk_forward(days, train_size=100, test_size=50)
    start, end = coverage(windows)

    #: 報告してよい期間は検証部分だけで、学習期間は含まない
    assert start == windows[0].test_start
    assert start > days[0]
    assert end == windows[-1].test_end


def test_validate_no_overlap_catches_hand_built_overlap():
    a = Window(0, dt.date(2020, 1, 1), dt.date(2020, 3, 1), dt.date(2020, 3, 2), dt.date(2020, 6, 1))
    b = Window(1, dt.date(2020, 2, 1), dt.date(2020, 4, 1), dt.date(2020, 4, 2), dt.date(2020, 7, 1))
    #: b の検証開始(4/2) は a の検証終了(6/1) より前なので重なっている
    with pytest.raises(ValueError, match="重なっています"):
        validate_no_overlap((a, b))


# ----------------------------------------------------------------------
# パラメータ選択と多重検定
# ----------------------------------------------------------------------
def test_expand_grid_counts_all_combinations():
    grid = {"fast": [5, 10, 20], "slow": [50, 100]}
    combos = expand_grid(grid)

    assert len(combos) == 6
    assert {"fast": 5, "slow": 50} in combos
    #: 空の格子でも1通り（パラメータなし）として扱う
    assert expand_grid({}) == [{}]


def test_expected_max_sharpe_grows_with_trial_count():
    """試行回数が増えるほど、まぐれの最大値は大きくなる。

    これを知らずに「50通り試して Sharpe 0.5 が出た」と喜ぶのが
    最もよくある失敗。
    """
    few = expected_max_sharpe(trials=2, sample_size=100)
    many = expected_max_sharpe(trials=100, sample_size=100)

    assert few is not None and many is not None
    assert many > few > 0


def test_expected_max_sharpe_shrinks_with_sample_size():
    """標本が多いほど、まぐれの幅は小さくなる。"""
    small = expected_max_sharpe(trials=20, sample_size=30)
    large = expected_max_sharpe(trials=20, sample_size=1000)

    assert small is not None and large is not None
    assert small > large


def test_single_candidate_has_no_selection_bias():
    selection = select_best([Candidate({"a": 1}, score=0.4)], sample_size=100)

    assert selection.trials == 1
    assert selection.expected_best_under_null == 0.0
    assert "選択バイアスはありません" in selection.note


def test_weak_best_is_flagged_as_indistinguishable_from_noise():
    """多数の候補から選んだわずかな優位は、偶然と区別がつかないこと。"""
    candidates = [Candidate({"i": i}, score=0.01 * i) for i in range(30)]
    selection = select_best(candidates, sample_size=40)

    assert selection.trials == 30
    assert selection.beats_null is False
    assert "超えていません" in selection.note


def test_strong_best_beats_the_null():
    candidates = [Candidate({"i": i}, score=0.05) for i in range(9)]
    candidates.append(Candidate({"i": 99}, score=3.0))
    selection = select_best(candidates, sample_size=200)

    assert selection.beats_null is True
    assert selection.params == {"i": 99}


def test_selection_records_filtered_candidates():
    """取引数不足で除外された数を失わないこと。

    「格子は9通りだが比較できたのは2通り」を混同すると、
    多重検定の補正が過大にも過小にもなる。
    """
    candidates = [Candidate({"i": i}, score=0.5 + i) for i in range(2)]
    selection = select_best(candidates, sample_size=100, attempted=9)

    assert selection.trials == 2
    assert selection.attempted == 9
    assert "除外" in selection.note


# ----------------------------------------------------------------------
# 過学習の診断
# ----------------------------------------------------------------------
def test_degradation_is_undefined_when_training_is_negative():
    """学習側がマイナスなら劣化率に意味がないこと。

    「マイナスがもっとマイナスになった」を 0.5 のような数字にすると誤読する。
    """
    assert degradation_ratio(-1.0, -2.0) is None
    assert degradation_ratio(2.0, 1.0) == pytest.approx(0.5)
    #: 検証のほうが良い場合は劣化0（マイナスにはしない）
    assert degradation_ratio(1.0, 2.0) == 0.0


def test_parameter_stability_detects_jumping_values():
    stable = parameter_stability([{"fast": 25}, {"fast": 25}, {"fast": 20}])
    unstable = parameter_stability([{"fast": 5}, {"fast": 100}, {"fast": 40}])

    assert stable[0].is_stable
    assert not unstable[0].is_stable


def test_diagnosis_needs_enough_windows():
    result = diagnose([1.0, 1.0], [0.9, 0.9], [{"a": 1}, {"a": 1}])

    assert result.verdict is Verdict.INSUFFICIENT
    assert any("窓が2個" in w for w in result.warnings)


def test_diagnosis_flags_heavy_degradation_as_overfit():
    train = [2.0] * 6
    test = [0.2] * 6
    result = diagnose(train, test, [{"fast": 25}] * 6)

    assert result.verdict is Verdict.OVERFIT
    assert result.degradation == pytest.approx(0.9)
    assert any("合わせ込んでいる" in w for w in result.warnings)


def test_diagnosis_flags_negative_test_score_as_overfit():
    result = diagnose([1.0] * 6, [-0.3] * 6, [{"fast": 25}] * 6)

    assert result.verdict is Verdict.OVERFIT
    assert any("優位性が残っていません" in w for w in result.warnings)


def test_diagnosis_flags_unstable_parameters_as_fragile():
    selections = [{"fast": v} for v in (5, 100, 10, 80, 20, 60)]
    result = diagnose([1.0] * 6, [0.9] * 6, selections)

    assert result.verdict is Verdict.FRAGILE
    assert any("安定していません" in w for w in result.warnings)


def test_diagnosis_never_claims_a_strategy_works():
    """最良でも「否定はされていない」までしか言わないこと。

    統計でできるのはここまで。「有効」と書いた瞬間に嘘になる。
    """
    result = diagnose([1.0] * 8, [0.95] * 8, [{"fast": 25}] * 8)

    assert result.verdict is Verdict.PLAUSIBLE
    assert "否定はされていない" in result.verdict.label
    assert any("「有効」という意味ではなく" in n for n in result.notes)
