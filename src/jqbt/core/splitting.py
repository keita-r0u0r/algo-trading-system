"""期間分割（純粋関数）。

## なぜ分割するのか

同じ期間でパラメータを決めて、同じ期間で成績を報告したら、その成績は
「最も都合のよい組み合わせを後から選んだ結果」でしかない。
未知の期間で残るかどうかは何も言っていない。

## 2つのやり方

**ホールドアウト**（``holdout``）
単純に前半で決めて後半で検証する。分かりやすいが、後半を1回使うと
そこはもう「未知の期間」ではなくなる。後半を見てパラメータを直したら、
その時点で後半も学習データになる。

**ウォークフォワード**（``walk_forward``）
窓をずらしながら「学習 → 検証」を繰り返し、検証期間だけを繋ぎ直す。

    |--- 学習1 ---|-検証1-|
           |--- 学習2 ---|-検証2-|
                  |--- 学習3 ---|-検証3-|

    検証1 + 検証2 + 検証3 が、報告してよい唯一の成績

実運用に近いのはこちら。実際の運用も「直近N年で決めて、次のM月で使い、
またN年で決め直す」の繰り返しになるため。

## anchored と rolling

- ``anchored=True``  : 学習の開始点を固定し、窓を伸ばしていく。データを無駄にしない
- ``anchored=False`` : 学習の幅を固定してずらす（既定）。相場の質の変化に追随する

どちらが正しいということはない。両方回して結果が大きく違うなら、
その戦略は相場環境に強く依存している、という情報が得られる。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Window:
    """学習期間と検証期間の組。

    ``train_end < test_start`` を不変条件として持つ。
    ここが崩れると学習データと検証データが重なり、検証の意味が消える。
    """

    index: int
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date

    def __post_init__(self) -> None:
        if not self.train_start <= self.train_end:
            raise ValueError("学習期間の開始が終了より後になっています")
        if not self.test_start <= self.test_end:
            raise ValueError("検証期間の開始が終了より後になっています")
        if not self.train_end < self.test_start:
            raise ValueError(
                "学習期間と検証期間が重なっています。"
                "重なると検証にならないので、必ず分離してください"
            )

    @property
    def train_days(self) -> int:
        return (self.train_end - self.train_start).days

    @property
    def test_days(self) -> int:
        return (self.test_end - self.test_start).days

    def label(self) -> str:
        return (
            f"#{self.index} 学習 {self.train_start}〜{self.train_end} / "
            f"検証 {self.test_start}〜{self.test_end}"
        )


# ----------------------------------------------------------------------
def holdout(dates: list[dt.date], train_ratio: float = 0.7) -> Window:
    """単純な前後分割。

    最初の1回だけ使う想定。ここでパラメータを直したら、検証期間も
    学習データになったことを忘れないこと。
    """
    if not 0.0 < train_ratio < 1.0:
        raise ValueError("train_ratio は 0 と 1 のあいだで指定してください")
    ordered = sorted(set(dates))
    if len(ordered) < 2:
        raise ValueError("分割するには最低2営業日必要です")

    cut = max(int(len(ordered) * train_ratio), 1)
    cut = min(cut, len(ordered) - 1)
    return Window(
        index=0,
        train_start=ordered[0],
        train_end=ordered[cut - 1],
        test_start=ordered[cut],
        test_end=ordered[-1],
    )


def walk_forward(
    dates: list[dt.date],
    train_size: int,
    test_size: int,
    step: int | None = None,
    anchored: bool = False,
) -> tuple[Window, ...]:
    """ウォークフォワードの窓を作る。

    単位は **営業日数**（暦日ではない）。渡した ``dates`` に含まれる
    営業日を数えるので、休場日の扱いを気にしなくてよい。

    ``step`` を省略すると ``test_size`` と同じになり、検証期間が
    隙間なく・重複なく並ぶ。**検証期間を重複させてはいけない。**
    重複させると同じ日を何度も成績に数えることになる。
    """
    ordered = sorted(set(dates))
    if train_size < 1 or test_size < 1:
        raise ValueError("train_size と test_size は1以上が必要です")
    step = step or test_size
    if step < 1:
        raise ValueError("step は1以上が必要です")

    windows: list[Window] = []
    start = 0
    index = 0
    while start + train_size + test_size <= len(ordered):
        train_from = 0 if anchored else start
        train_to = start + train_size - 1
        test_from = train_to + 1
        test_to = min(test_from + test_size - 1, len(ordered) - 1)

        windows.append(
            Window(
                index=index,
                train_start=ordered[train_from],
                train_end=ordered[train_to],
                test_start=ordered[test_from],
                test_end=ordered[test_to],
            )
        )
        index += 1
        start += step

    return tuple(windows)


def coverage(windows: tuple[Window, ...]) -> tuple[dt.date, dt.date] | None:
    """検証期間全体の範囲。報告してよい成績の期間はここ。"""
    if not windows:
        return None
    return windows[0].test_start, windows[-1].test_end


def validate_no_overlap(windows: tuple[Window, ...]) -> None:
    """検証期間が重なっていないことを確かめる。

    重なりは「同じ日を複数回カウントする」という形で成績を水増しする。
    自分で窓を作るときの安全弁。
    """
    for previous, current in zip(windows, windows[1:]):
        if current.test_start <= previous.test_end:
            raise ValueError(
                f"検証期間が重なっています: "
                f"#{previous.index}({previous.test_end}) と "
                f"#{current.index}({current.test_start})"
            )


#: 窓がこれ未満だと、ウォークフォワードの結果は偶然に支配される
MIN_RELIABLE_WINDOWS = 5
