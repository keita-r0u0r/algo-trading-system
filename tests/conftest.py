"""テスト共通のヘルパー。"""

from __future__ import annotations

import pandas as pd
import pytest


def equity_of(result) -> pd.Series:
    """結果から資産曲線を取り出す。"""
    return pd.Series(
        [s.equity for s in result.snapshots],
        index=pd.DatetimeIndex([s.date for s in result.snapshots]),
        dtype=float,
    )


@pytest.fixture
def equity():
    return equity_of
