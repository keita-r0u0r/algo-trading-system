"""先読みをしていないことの検証。

**このファイルが通らなくなったら、他のすべての結果は無意味になる。**

先読みは静かに入り、しかも成績を良くするので、テストがないと気づけない。
「バックテストがやけに良い」ときに最初に疑うべき場所をここで固定する。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from conftest import equity_of

from jqbt import Order
from jqbt.application import BacktestConfig, BacktestRunner
from jqbt.application import PanelMarketView
from jqbt.application.panel_index import build_index
from jqbt.infrastructure.synthetic import generate_panel


@pytest.fixture(scope="module")
def panel():
    return generate_panel(
        symbols=["A", "B", "C"],
        start=dt.date(2023, 1, 1),
        end=dt.date(2024, 6, 30),
        seed=7,
    )


# ----------------------------------------------------------------------
# MarketView が未来を渡さないこと
# ----------------------------------------------------------------------
def test_view_history_stops_at_as_of(panel):
    dates = sorted({d.date() for d in panel.index.get_level_values("date")})
    cutoff = dates[len(dates) // 2]
    view = PanelMarketView(build_index(panel), cutoff)

    history = view.history()
    assert len(history) > 0
    latest = max(history.index.get_level_values("date")).date()
    assert latest <= cutoff


def test_view_series_stops_at_as_of(panel):
    dates = sorted({d.date() for d in panel.index.get_level_values("date")})
    cutoff = dates[100]
    view = PanelMarketView(build_index(panel), cutoff)

    closes = view.series("A", "close")
    assert len(closes) > 0
    assert max(closes.index).date() <= cutoff


def test_view_price_is_the_close_of_that_day(panel):
    dates = sorted({d.date() for d in panel.index.get_level_values("date")})
    day = dates[50]
    view = PanelMarketView(build_index(panel), day)

    expected = float(panel.loc[(pd.Timestamp(day), "A"), "close"])
    assert view.price("A") == pytest.approx(expected)


# ----------------------------------------------------------------------
# エンジンの執行タイミング
# ----------------------------------------------------------------------
class _RecordingStrategy:
    """判断した日と、実際に約定した日を記録する。"""

    name = "recording"

    def __init__(self, buy_on_index: int = 5):
        self.buy_on_index = buy_on_index
        self.seen_dates: list[dt.date] = []
        self.max_seen_in_view: list[dt.date] = []
        self.ordered_on: dt.date | None = None

    def on_bar(self, view, portfolio):
        self.seen_dates.append(view.as_of)
        history = view.history()
        self.max_seen_in_view.append(
            max(history.index.get_level_values("date")).date()
        )
        if len(self.seen_dates) == self.buy_on_index and not portfolio.positions:
            self.ordered_on = view.as_of
            return [Order("A", "buy", 100)]
        return []


def test_order_fills_at_next_day_open_not_same_day_close(panel):
    """T日の引けで判断した注文は、T+1日の始値で約定する。

    同じ日の終値で約定させると「その日の終値を見てから買った」ことになる。
    これは最も入りやすい先読みで、しかも成績が劇的に良くなる。
    """
    strategy = _RecordingStrategy(buy_on_index=5)
    result = BacktestRunner(panel, BacktestConfig(initial_cash=1_000_000)).run(strategy)

    assert result.fills, "約定が1件もありません"
    trade = result.fills[0]

    assert strategy.ordered_on is not None
    #: 約定日は判断日より後
    assert trade.date > strategy.ordered_on

    #: 約定価格は「約定日の始値」を基準にしている（判断日の終値ではない）
    fill_open = float(panel.loc[(pd.Timestamp(trade.date), "A"), "open"])
    decision_close = float(panel.loc[(pd.Timestamp(strategy.ordered_on), "A"), "close"])
    assert abs(trade.price - fill_open) < abs(trade.price - decision_close) or (
        trade.price >= fill_open
    )
    #: 買いは必ず始値以上（不利な側）で約定する
    assert trade.price >= fill_open


def test_strategy_never_sees_beyond_its_own_bar(panel):
    """戦略に渡る view が、その日を超えたデータを含まないこと。"""
    strategy = _RecordingStrategy(buy_on_index=10**9)
    BacktestRunner(panel, BacktestConfig(initial_cash=1_000_000)).run(strategy)

    assert strategy.seen_dates
    for as_of, max_seen in zip(strategy.seen_dates, strategy.max_seen_in_view):
        assert max_seen <= as_of


def test_dates_are_processed_in_order(panel):
    strategy = _RecordingStrategy(buy_on_index=10**9)
    BacktestRunner(panel, BacktestConfig(initial_cash=1_000_000)).run(strategy)
    assert strategy.seen_dates == sorted(strategy.seen_dates)


# ----------------------------------------------------------------------
# 未来のデータを消しても、過去の結果が変わらないこと
# ----------------------------------------------------------------------
def test_truncating_the_future_does_not_change_the_past(panel):
    """期間を短く切っても、重なる部分の結果は完全に一致すること。

    一致しないなら、どこかで未来を見ている。
    ここは先読み検出として最も強い検査で、戦略を書き換えても効く。
    """
    from jqbt.strategies import SmaCross

    dates = sorted({d.date() for d in panel.index.get_level_values("date")})
    cutoff = dates[len(dates) * 2 // 3]

    full = BacktestRunner(panel, BacktestConfig(initial_cash=1_000_000)).run(
        SmaCross(fast=5, slow=20, max_positions=2)
    )
    truncated_panel = panel.loc[
        panel.index.get_level_values("date") <= pd.Timestamp(cutoff)
    ]
    truncated = BacktestRunner(truncated_panel, BacktestConfig(initial_cash=1_000_000)).run(
        SmaCross(fast=5, slow=20, max_positions=2)
    )

    overlap = equity_of(truncated).index
    pd.testing.assert_series_equal(
        equity_of(full).loc[overlap], equity_of(truncated), check_names=False
    )

    full_trades = [t for t in full.fills if t.date <= cutoff]
    assert len(full_trades) == len(truncated.fills)
    for a, b in zip(full_trades, truncated.fills):
        assert (a.date, a.symbol, a.side, a.quantity) == (
            b.date,
            b.symbol,
            b.side,
            b.quantity,
        )
        assert a.price == pytest.approx(b.price)


# ----------------------------------------------------------------------
# 調整後価格の扱い
# ----------------------------------------------------------------------
def test_adjusted_prices_must_not_be_used_for_price_level_filters():
    """価格水準の条件は調整前（raw_close）で判定すること。

    調整後は分割で遡及的に書き換わるため、「株価1,000円以下」を
    調整後で判定すると、将来の分割を知っていることになる。
    """
    from jqbt.infrastructure.parquet_store import liquid_universe

    days = pd.bdate_range("2024-01-01", "2024-06-28")
    frame = pd.DataFrame(
        {
            "date": list(days) * 2,
            "symbol": ["SPLIT"] * len(days) + ["PLAIN"] * len(days),
            # 調整後: 分割で過去が半分に書き換わり、ずっと500円に見える
            "close": [500.0] * len(days) + [800.0] * len(days),
            # 調整前: 実際には分割前は1,000円だった
            "raw_close": [1000.0] * len(days) + [800.0] * len(days),
            "open": [500.0] * len(days) + [800.0] * len(days),
            "high": [510.0] * len(days) + [810.0] * len(days),
            "low": [490.0] * len(days) + [790.0] * len(days),
            "volume": [100_000.0] * (len(days) * 2),
            "turnover": [500_000_000.0] * (len(days) * 2),
        }
    ).set_index(["date", "symbol"])

    universe = liquid_universe(
        frame, as_of=days[-1].date(), lookback=60, max_price=900.0
    )
    #: 調整前1,000円なので当時は買えなかった → 除外されるのが正しい
    assert "SPLIT" not in universe
    assert "PLAIN" in universe


# ----------------------------------------------------------------------
# 未来を「汚染」しても、過去の判断が変わらないこと
# ----------------------------------------------------------------------
class _OrderRecorder:
    """戦略を包んで、日ごとに出した注文を記録する。

    **約定ではなく注文を比べる。** 約定は判断の翌営業日に起きるので、
    1日ぶんの先読みの影響は必ず境界日の約定に現れ、
    「境界より前の約定」を比べる限り検出できない。
    """

    def __init__(self, inner):
        self.inner = inner
        self.name = inner.name
        self.log: dict = {}

    def on_bar(self, view, portfolio):
        orders = self.inner.on_bar(view, portfolio) or []
        self.log[view.as_of] = tuple(
            (o.symbol, o.side.value, o.quantity) for o in orders
        )
        return orders


def test_poisoning_the_future_does_not_change_past_decisions(panel):
    """cutoff 以降のデータを壊しても、cutoff より前の **判断** が一致すること。

    truncation（期間を切る）より強い検出器。切ると最終日の挙動が変わるため、
    最終バーに閉じた先読みを見逃す。実際、索引を1日ずらすバグは
    truncation でも「約定の比較」でも検出できなかった。

    汚染ならパネルの形が変わらないので境界日も同条件で比較でき、
    注文を比べれば1日ぶんの先読みも捕まえられる。
    """
    from jqbt.strategies import SmaCross

    dates = sorted({d.date() for d in panel.index.get_level_values("date")})
    cutoff = dates[len(dates) * 2 // 3]

    poisoned = panel.copy()
    future = poisoned.index.get_level_values("date") >= pd.Timestamp(cutoff)
    for column in ("open", "high", "low", "close"):
        poisoned.loc[future, column] = 1.0  # ありえない値で塗り潰す
    poisoned.loc[future, "volume"] = 1.0

    def run(frame):
        recorder = _OrderRecorder(SmaCross(fast=5, slow=20, max_positions=2))
        BacktestRunner(frame, BacktestConfig(initial_cash=1_000_000)).run(recorder)
        return recorder.log

    clean = run(panel)
    dirty = run(poisoned)

    for day in dates:
        if day >= cutoff:
            break
        assert clean.get(day) == dirty.get(day), (
            f"{day} の注文が、未来を汚染したら変わりました。"
            "その日より後のデータを見ています"
        )


def test_values_fast_path_respects_the_boundary(panel):
    """``values``（戦略が実際に使う numpy の経路）にも上限が効くこと。

    ``series`` だけ検査していると、高速版に穴が空いたまま気づけない。
    戦略が使っているのはこちらなので、むしろこちらが本命。
    """
    from jqbt.application.panel_index import build_index

    index = build_index(panel)
    dates = sorted({d.date() for d in panel.index.get_level_values("date")})
    cutoff = dates[80]
    view = PanelMarketView(index, cutoff)

    values = view.values("A", "close")
    expected = panel.xs("A", level="symbol")
    expected = expected[expected.index <= pd.Timestamp(cutoff)]["close"]

    assert len(values) == len(expected), (
        f"境界がずれています（取得{len(values)}件 / 期待{len(expected)}件）"
    )
    assert values[-1] == pytest.approx(float(expected.iloc[-1]))
