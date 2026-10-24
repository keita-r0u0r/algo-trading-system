"""FX エンジンの約束: 約定価格、損切り/利確の順番、ロスカット、日をまたがない手仕舞い。

ここが1円でもずれると、その先の検証はすべて無意味になる。手で検算できる足で固定する。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from jqbt.application.fx_backtest import FxConfig, run_fx
from jqbt.core.fx.model import (
    FxCosts,
    FxInstrument,
    FxPosition,
    FxSignal,
    losscut_price,
    maintenance_ratio,
    stop_or_take,
    unrealized,
)

INST = FxInstrument(min_units=1_000, leverage=25, losscut_ratio=0.5)
ZERO = FxCosts(spread_pips=0, slippage_pips=0, stop_slippage_pips=0)
COSTS = FxCosts(spread_pips=0.2, slippage_pips=0.1, stop_slippage_pips=0.3)


def bars(rows, start="2025-01-06 00:00"):
    """rows: [(open, high, low, close), ...] を1分足に。"""
    index = pd.date_range(start, periods=len(rows), freq="1min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


class Script:
    """決めた足で、決めた判断を返す。"""

    name = "script"

    def __init__(self, plan: dict[int, FxSignal]):
        self.plan = plan

    def decide(self, view, position):
        return self.plan.get(view.index)


def config(**kwargs):
    base = dict(initial_cash=100_000.0, instrument=INST, costs=ZERO, no_entry_last_bars=0, flat_at_day_end=False)
    base.update(kwargs)
    return FxConfig(**base)


# ----------------------------------------------------------------------
def test_buy_at_ask_sell_at_bid():
    """スプレッド0.2pips + ずれ0.1pips: 買いは仲値 +0.2pips、売りは仲値 −0.2pips。"""
    assert COSTS.fill(150.0, True, INST) == pytest.approx(150.002)
    assert COSTS.fill(150.0, False, INST) == pytest.approx(149.998)


def test_round_trip_pnl_is_exact_without_costs():
    data = bars([(150, 150, 150, 150), (150.0, 150.2, 150.0, 150.1), (150.2, 150.2, 150.2, 150.2), (150.3, 150.3, 150.3, 150.3)])
    result = run_fx(data, Script({0: FxSignal(1), 2: FxSignal(0)}), config())
    (trade,) = result.trades
    # 足1の始値150.0で買い → 足3の始値150.3で売り。1,000通貨 × 0.3円 = 300円
    assert trade.entry_price == pytest.approx(150.0)
    assert trade.exit_price == pytest.approx(150.3)
    assert trade.pnl == pytest.approx(300.0)
    assert result.equity[-1] == pytest.approx(100_300.0)


def test_short_profits_when_price_falls():
    data = bars([(150, 150, 150, 150), (150.0, 150.0, 149.8, 149.9), (149.7, 149.7, 149.7, 149.7)])
    result = run_fx(data, Script({0: FxSignal(-1), 1: FxSignal(0)}), config())
    assert result.trades[0].pnl == pytest.approx((150.0 - 149.7) * 1_000)


def test_costs_are_paid_on_both_legs():
    data = bars([(150, 150, 150, 150), (150, 150, 150, 150), (150, 150, 150, 150)])
    result = run_fx(data, Script({0: FxSignal(1), 1: FxSignal(0)}), config(costs=COSTS))
    # 値動き0で往復: 買い150.002、売り149.998 → -0.004円 × 1,000 = -4円
    assert result.trades[0].pnl == pytest.approx(-4.0)


def test_when_both_stop_and_take_are_touched_the_stop_wins():
    """1本の足で両方に届いたら、損切りが先と見なす（有利な側を先にすると楽観的になる）。"""
    position = FxPosition(units=1_000, price=150.0, stop=149.9, take=150.1)
    price, reason = stop_or_take(position, 150.0, 150.2, 149.8, ZERO, INST)
    assert reason == "損切り" and price == pytest.approx(149.9)


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    """窓開けで逆指値を飛び越えたら、逆指値ではなく、より不利な始値で約定する。"""
    position = FxPosition(units=1_000, price=150.0, stop=149.9)
    price, reason = stop_or_take(position, 149.5, 149.6, 149.4, ZERO, INST)
    assert reason == "損切り" and price == pytest.approx(149.5)


def test_short_stop_uses_the_ask_side():
    position = FxPosition(units=-1_000, price=150.0, stop=150.1)
    # 仲値の高値150.09でも、買値は150.09 + 0.001 = 150.091 で、逆指値150.1には届かない
    assert stop_or_take(position, 150.0, 150.09, 149.99, COSTS, INST) is None
    # 仲値の高値150.1なら、買値150.101で届く
    assert stop_or_take(position, 150.0, 150.1, 149.99, COSTS, INST) is not None


def test_losscut_price_is_where_the_maintenance_ratio_hits_the_threshold():
    position = FxPosition(units=1_000, price=150.0)
    cash = 10_000.0
    level = losscut_price(cash, position, ZERO, INST)
    equity = cash + unrealized(position, level, ZERO, INST)
    assert maintenance_ratio(equity, position, level, INST) == pytest.approx(0.5)
    # 1万円・1,000通貨・25倍・基準50%なら、約7円の逆行でロスカット
    assert 150.0 - level == pytest.approx(7.0, abs=0.2)


def test_losscut_ends_the_run():
    rows = [(150, 150, 150, 150), (150, 150, 150, 150)] + [(150, 150, 140, 141)] + [(141, 141, 141, 141)] * 5
    result = run_fx(bars(rows), Script({0: FxSignal(1)}), config(initial_cash=10_000.0))
    assert result.losscut
    assert result.trades[-1].exit_reason == "強制ロスカット"
    # 終わった後は、評価額が動かない（検証の強制終了）
    assert len(set(result.equity[3:])) == 1


def test_a_gap_beyond_the_losscut_can_leave_a_negative_balance():
    """ロスカットでも、窓開けでは入金額を超えて損をすることがある（不足金）。"""
    rows = [(150, 150, 150, 150), (150, 150, 150, 150), (135, 135, 134, 134), (134, 134, 134, 134)]
    result = run_fx(bars(rows), Script({0: FxSignal(1)}), config(initial_cash=10_000.0))
    assert result.losscut
    assert result.summary["negative_balance"]


def test_positions_are_closed_at_the_end_of_the_day():
    day1 = [(150, 150, 150, 150)] * 3
    data = pd.concat([bars(day1, "2025-01-06 23:57"), bars([(151, 151, 151, 151)] * 2, "2025-01-07 00:00")])
    result = run_fx(data, Script({0: FxSignal(1)}), config(flat_at_day_end=True))
    assert result.trades and result.trades[0].exit_reason == "日の終わり"
    assert result.trades[0].exit_at.date() == pd.Timestamp("2025-01-06").date()


def test_no_new_positions_just_before_the_day_ends():
    data = bars([(150, 150, 150, 150)] * 10, "2025-01-06 23:50")
    result = run_fx(data, Script({7: FxSignal(1)}), config(flat_at_day_end=True, no_entry_last_bars=5))
    assert not result.trades


def test_entries_are_skipped_when_the_margin_is_not_enough():
    """1万円で1,000通貨（必要証拠金6,000円）。資金の80%を超える建玉は作らない。"""
    data = bars([(150, 150, 150, 150)] * 4)
    result = run_fx(data, Script({0: FxSignal(1)}), config(initial_cash=7_000.0))
    assert not result.trades and result.skipped_entries == 1


def test_strategies_see_only_the_past():
    seen = []

    class Spy:
        name = "spy"

        def decide(self, view, position):
            seen.append((view.index, len(view.close), float(view.close[-1])))
            return None

    data = bars([(i, i, i, i) for i in range(100, 105)])
    run_fx(data, Spy(), config())
    assert all(length == index + 1 and last == 100 + index for index, length, last in seen)


# --- 実際の相場のデータ（GMOコイン）に合わせた約束 ------------------------------
def test_fee_rate_makes_fills_worse():
    """手数料 0.002%: 150円なら1通貨あたり 0.003円、不利な側へずれる。"""
    fee = FxCosts(spread_pips=0, slippage_pips=0, stop_slippage_pips=0, fee_rate=0.00002)
    assert fee.fill(150.0, True, INST) == pytest.approx(150.003)
    assert fee.fill(150.0, False, INST) == pytest.approx(149.997)


def test_fee_applies_to_stop_and_take():
    fee = FxCosts(spread_pips=0, slippage_pips=0, stop_slippage_pips=0, fee_rate=0.00002)
    long = FxPosition(units=1_000, price=150.0, stop=149.9, take=150.2)
    price, reason = stop_or_take(long, 150.0, 150.0, 149.8, fee, INST)
    assert reason == "損切り" and price == pytest.approx(149.9 - 149.9 * 0.00002)
    price, reason = stop_or_take(long, 150.0, 150.3, 150.0, fee, INST)
    assert reason == "利確" and price == pytest.approx(150.2 - 150.2 * 0.00002)


def test_entry_uses_entry_leverage():
    """新規は20倍まで: 1,000通貨 × 150円 ÷ 20 = 7,500円。資金 9,000円 × 80% = 7,200円では作れない。"""
    inst = FxInstrument(min_units=1_000, leverage=25, losscut_ratio=1.0, entry_leverage=20)
    assert inst.entry_margin(1_000, 150.0) == pytest.approx(7_500)
    data = bars([(150, 150, 150, 150)] * 3)
    result = run_fx(data, Script({0: FxSignal(1)}), config(initial_cash=9_000.0, instrument=inst))
    assert result.skipped_entries == 1 and not result.trades


def test_session_column_defines_the_day():
    """営業日は朝6時に始まる。日付が変わっても同じ営業日なら手仕舞わない。"""
    data = bars([(150, 150, 150, 150)] * 4, start="2025-01-06 23:58")
    data["session"] = [pd.Timestamp("2025-01-06").date()] * 3 + [pd.Timestamp("2025-01-07").date()]
    result = run_fx(data, Script({0: FxSignal(1)}), config(flat_at_day_end=True))
    assert len(result.trades) == 1
    assert result.trades[0].exit_at == data.index[2] and result.trades[0].exit_reason == "日の終わり"


def test_per_bar_spread_is_used_for_fills():
    data = bars([(150, 150, 150, 150), (150, 150, 150, 150), (150, 150, 150, 150)])
    data["spread"] = [0.2, 2.0, 0.2]
    result = run_fx(data, Script({0: FxSignal(1), 1: FxSignal(0)}), config())
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(150.01)  # 足1の始値、スプレッド 2.0pips の半分
    assert trade.exit_price == pytest.approx(149.999)  # 足2の始値、スプレッド 0.2pips の半分


def test_swap_is_paid_per_calendar_day_when_held_overnight():
    """金曜の営業日から月曜の営業日へ持ち越すと、3日分のスワップ。"""
    data = bars([(150, 150, 150, 150)] * 4, start="2025-01-10 23:58")
    data["session"] = [pd.Timestamp("2025-01-10").date()] * 2 + [pd.Timestamp("2025-01-13").date()] * 2
    result = run_fx(data, Script({0: FxSignal(1)}), config(swap_long=0.01))
    # 1,000通貨 × 0.01円 × 3日 = 30円。期間の終わりに手仕舞う
    assert result.trades[0].exit_reason == "期間の終わり"
    assert result.summary["final"] == pytest.approx(100_000 + 30)


def test_gotobi_days_move_weekends_to_friday():
    import datetime as dt

    from jqbt.strategies.fx import gotobi_days

    days = gotobi_days(2026, 10)
    assert dt.date(2026, 10, 9) in days  # 10日は土曜 → 9日（金）
    assert dt.date(2026, 10, 23) in days  # 25日は日曜 → 23日（金）
    assert dt.date(2026, 2, 27) in gotobi_days(2026, 2)  # 2月は月末（28日は土曜 → 27日）


# --- リスクの度合い ----------------------------------------------------------
def test_size_units_from_risk_and_leverage():
    from jqbt.core.fx.model import RISK_PROFILES, size_units

    inst = FxInstrument(min_units=100)
    # 中: 1万円 × 1% = 100円 ÷ (20 pips × 0.01円) = 500通貨。レバレッジ5倍の上限（約316通貨）で切られる
    assert size_units(RISK_PROFILES["medium"], 10_000, 158.0, 20, inst) == 316
    # 最大: 250円 ÷ 0.2円 = 1,250通貨（20倍の上限 1,265通貨より小さい）
    assert size_units(RISK_PROFILES["max"], 10_000, 158.0, 20, inst) == 1_250
    # 小: 50円 ÷ (100 pips × 0.01円) = 50通貨 < 最小100 → 入らない
    assert size_units(RISK_PROFILES["small"], 10_000, 158.0, 100, inst) == 0


def test_daily_loss_limit_closes_and_halts_for_the_day():
    from jqbt.core.fx.model import RiskProfile

    risk = RiskProfile("test", risk_per_trade=1.0, max_leverage=100, daily_loss_limit=0.01, assumed_stop_pips=1000)
    inst = FxInstrument(min_units=1, leverage=25, losscut_ratio=0.0)
    # 1万円 × 100% ÷ (1000 pips × 0.01円) = 1,000通貨。1円下がると 1,000円（10%）の損で上限に届く
    data = bars([(150, 150, 150, 150), (150, 150, 150, 150), (149, 149, 149, 149), (149, 149, 149, 149), (149, 149, 149, 149)])
    plan = {0: FxSignal(1), 2: FxSignal(1)}
    result = run_fx(data, Script(plan), config(initial_cash=10_000.0, instrument=inst, risk=risk))
    assert result.trades[0].units == 1_000
    assert result.trades[0].exit_reason == "1日の損失の上限"
    assert len(result.trades) == 1 and result.skipped_entries == 1  # その日はもう入らない


def test_no_trade_keeps_cash():
    from jqbt.strategies.fx import NoTradeFx

    data = bars([(150, 151, 149, 150)] * 5)
    result = run_fx(data, NoTradeFx(), config())
    assert result.summary["total_return"] == 0 and not result.trades


def test_rate_momentum_uses_only_rates_known_before_the_day():
    from jqbt.strategies.fx import RateMomentum

    dates = pd.bdate_range("2025-01-01", periods=30)
    spread = pd.Series([1.0] * 25 + [1.5] * 5, index=dates)
    rule = RateMomentum(spread, lookback=20, band=0.1)
    day = dates[25].date()
    assert rule.target(day) == 0  # その日の値（1.5 への上昇）はまだ分からない
    assert rule.target(dates[26].date()) == 1  # 前の日までに 1.0 → 1.5（+0.5%）


def test_wareki_dates():
    import datetime as dt

    from jqbt.infrastructure.rates import parse_wareki

    assert parse_wareki("R8.9.30") == dt.date(2026, 9, 30)
    assert parse_wareki("H31.4.30") == dt.date(2019, 4, 30)
    assert parse_wareki("基準日") is None


def test_daily_breakout_enters_after_close_above_20_day_high():
    from jqbt.strategies.fx import DailyBreakout

    # 22営業日。毎日 10:00 の1本だけ（高値 150.5・安値 149.5）。最後の日だけ 151 で引ける
    rows, index = [], []
    days = pd.bdate_range("2025-01-06", periods=23)
    for k, day in enumerate(days):
        close = 151.0 if k == 21 else 150.0
        for hhmm in ("09:00", "10:00"):
            index.append(pd.Timestamp(f"{day.date()} {hhmm}"))
            rows.append((150.0, max(150.5, close), 149.5, close))
    data = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(index))
    rule = DailyBreakout(window=20)
    signals = []
    for i in range(len(data)):
        from jqbt.application.fx_backtest import FxView
        view = FxView(i, data.index[i], data["open"].to_numpy()[: i + 1], data["high"].to_numpy()[: i + 1],
                      data["low"].to_numpy()[: i + 1], data["close"].to_numpy()[: i + 1], 10)
        s = rule.decide(view, FxPosition())
        if s is not None:
            signals.append((data.index[i], s.target))
    # 22日目（151 で引けた日）の翌朝9時に買い。それより前には何も出さない
    assert signals == [(pd.Timestamp(f"{days[22].date()} 09:00"), 1)]


def test_warmup_bars_feed_strategy_but_do_not_trade():
    seen = []

    class Watch:
        name = "watch"

        def decide(self, view, position):
            seen.append(view.index)
            return FxSignal(1)  # 毎回買いたがる

    data = bars([(150, 150, 150, 150)] * 6)
    result = run_fx(data, Watch(), config(trade_from=data.index[3]))
    assert seen[:3] == [0, 1, 2]  # 助走の足も見ている
    # 助走中の判断は捨てる。最初の約定は、助走のあと最初の判断（足3）を足4の始値で
    assert result.trades[0].entry_at == data.index[4]


def test_swap_from_columns_and_formula():
    from jqbt.core.fx.model import swap_per_unit

    long, short = swap_per_unit(158.0, 4.0, 1.5, markup=0.5)
    assert long == pytest.approx(158 * 0.02 / 365) and short == pytest.approx(-158 * 0.03 / 365)
    data = bars([(150, 150, 150, 150)] * 4, start="2025-01-10 23:58")
    data["session"] = [pd.Timestamp("2025-01-10").date()] * 2 + [pd.Timestamp("2025-01-13").date()] * 2
    data["swap_long"], data["swap_short"] = 0.02, -0.03
    result = run_fx(data, Script({0: FxSignal(1)}), config(swap_long=0.0))
    assert result.summary["final"] == pytest.approx(100_000 + 1_000 * 0.02 * 3)  # 列の値が使われる


def test_carry_filter_trend_needs_positive_carry_and_uptrend():
    from jqbt.application.fx_backtest import FxView
    from jqbt.strategies.fx import CarryFilter

    days = pd.bdate_range("2025-01-06", periods=60)
    carry = pd.Series(1.0, index=days)
    rule = CarryFilter(carry, "trend")
    out = []
    for k, day in enumerate(days):
        price = 150.0 + k * 0.1  # 毎日上がる
        for hhmm in ("09:00", "10:00"):
            t = pd.Timestamp(f"{day.date()} {hhmm}")
            view = FxView(0, t, np.array([price]), np.array([price]), np.array([price]), np.array([price]), 10)
            s = rule.decide(view, FxPosition())
            if s is not None:
                out.append((k, s.target))
    assert out and out[0][1] == 1 and out[0][0] == 50  # 50営業日分の終値（0〜49日目）がそろった翌朝
    assert CarryFilter(-carry, "trend")._carry_before(days[10].date()) == -1.0
