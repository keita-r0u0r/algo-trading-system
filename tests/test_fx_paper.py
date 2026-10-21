"""FX の前向きのペーパー運用: 判断の枠、仮想の約定、損切りの確認。通信はしない。"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from jqbt.application.fx_paper import PaperAccount, PaperPosition, advance, execute, hourly_breakout_target, mark
from jqbt.core.fx.frame import Approved, TradingFrame, check
from jqbt.core.fx.model import RISK_PROFILES, FxCosts, FxInstrument

INST = FxInstrument(min_units=100, leverage=25, losscut_ratio=1.0, entry_leverage=20)
FRAME = TradingFrame(symbols=("USD_JPY", "EUR_JPY"), risk=RISK_PROFILES["medium"])
ZERO = FxCosts(spread_pips=0, slippage_pips=0, stop_slippage_pips=0)


def _check(decision, **kw):
    base = dict(equity=10_000.0, holding=None, spread_pips={"USD_JPY": 0.2, "EUR_JPY": 0.5},
                prices={"USD_JPY": 158.0, "EUR_JPY": 170.0}, instruments={"USD_JPY": INST, "EUR_JPY": INST},
                halted=False)
    base.update(kw)
    return check(decision, FRAME, **base)


def open_long(**kw):
    return {"action": "open_long", "symbol": "USD_JPY", "risk_fraction": 0.01, "stop_pips": 20, "take_pips": 40, **kw}


# --- 枠 -------------------------------------------------------------------
def test_frame_sizes_from_risk_and_caps_at_profile():
    approved, reason = _check(open_long(risk_fraction=0.5))  # 50% は上限（中: 1%）に切られる
    assert reason == "" and approved.risk_fraction == 0.01
    # 1万円 × 1% ÷ (20 pips × 0.01円) = 500通貨。5倍の上限 316通貨で切られる
    assert approved.units == 316


def test_frame_rejects_outside_the_rules():
    assert _check(open_long(symbol="TRY_JPY"))[1].startswith("枠に無い通貨ペア")
    assert _check(open_long(stop_pips=0))[1].startswith("損切りの幅が枠の外")
    assert _check(open_long(stop_pips=500))[1].startswith("損切りの幅が枠の外")
    assert _check(open_long(take_pips=5))[1].startswith("利確の幅が小さすぎる")
    assert _check(open_long(), spread_pips={"USD_JPY": 3.0})[1].startswith("スプレッドが広い")
    assert _check(open_long(), halted=True)[1].startswith("1日の損失")
    assert _check(open_long(), holding="EUR_JPY")[1].startswith("建玉")
    assert _check({"action": "buy_everything"})[1].startswith("知らない判断")


def test_close_is_always_allowed_when_holding():
    approved, _ = _check({"action": "close"}, holding="USD_JPY", halted=True, spread_pips={"USD_JPY": 9.0})
    assert approved == Approved("close", "USD_JPY", 0.0, None, None, 0.0)
    assert _check({"action": "close"})[1] == "閉じる建玉が無い"


def test_hold_is_not_an_error():
    assert _check({"action": "hold"}) == (None, "")


# --- 約定と確認 ---------------------------------------------------------------
def test_execute_fills_at_ask_with_fee_and_slippage():
    costs = FxCosts(slippage_pips=0.1, fee_rate=0.00002)
    acc = PaperAccount("ai", 10_000, 10_000)
    step = execute(acc, Approved("open_long", "USD_JPY", 300, 20, 40, 0.01), bid=158.000, ask=158.002,
                   at=dt.datetime(2026, 10, 21, 10, 0), costs=costs, inst=INST, reason="test")
    p = step.account.position
    assert p.price == pytest.approx(158.002 + 0.001 + 158.002 * 0.00002)
    assert p.stop == pytest.approx(p.price - 0.20) and p.take == pytest.approx(p.price + 0.40)
    assert mark(step.account, 158.5, 158.502) == pytest.approx(10_000 + (158.5 - p.price) * 300)


def _bars(rows, start="2026-10-21 10:01", session="2026-10-21"):
    index = pd.date_range(start, periods=len(rows), freq="1min")
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)
    frame["spread"] = 0.0
    frame["session"] = dt.date.fromisoformat(session)
    return frame


def test_advance_hits_stop_on_bars_since_last_run():
    acc = PaperAccount("ai", 10_000, 10_000, PaperPosition("USD_JPY", 300, 158.0, 157.8, None, "x"),
                       last_time="2026-10-21T10:00:00", session="2026-10-21", day_start_equity=10_000)
    bars = _bars([(158.0, 158.1, 157.9, 158.0), (158.0, 158.0, 157.7, 157.75), (157.7, 157.7, 157.7, 157.7)])
    step = advance(acc, bars, ZERO, INST, daily_loss_limit=0.04)
    close = next(e for e in step.events if e["kind"] == "close")
    assert close["reason"] == "損切り" and close["exit_price"] == pytest.approx(157.8)
    assert step.account.cash == pytest.approx(10_000 - 0.2 * 300)
    assert step.account.last_time == bars.index[-1].isoformat()


def test_advance_ignores_bars_already_seen():
    acc = PaperAccount("ai", 10_000, 10_000, PaperPosition("USD_JPY", 300, 158.0, 157.8, None, "x"),
                       last_time="2026-10-21T10:02:00", session="2026-10-21", day_start_equity=10_000)
    bars = _bars([(158.0, 158.0, 157.0, 157.0), (158.0, 158.0, 157.0, 157.0)])  # 10:01, 10:02 は見た
    assert not advance(acc, bars, ZERO, INST, daily_loss_limit=0.04).events


def test_advance_daily_loss_limit_closes_and_halts():
    acc = PaperAccount("ai", 10_000, 10_000, PaperPosition("USD_JPY", 1_000, 158.0, 150.0, None, "x"),
                       last_time="2026-10-21T10:00:00", session="2026-10-21", day_start_equity=10_000)
    bars = _bars([(157.5, 157.5, 157.5, 157.5)])  # 0.5円 × 1,000通貨 = 500円（5%）の含み損
    step = advance(acc, bars, ZERO, INST, daily_loss_limit=0.04)
    assert [e["kind"] for e in step.events] == ["close", "halted"]
    assert step.account.position is None and step.account.halted_session == "2026-10-21"


def test_swap_on_session_change():
    acc = PaperAccount("ai", 10_000, 10_000, PaperPosition("USD_JPY", 100, 158.0, 150.0, None, "x"),
                       last_time="2026-10-23T23:00:00", session="2026-10-23", day_start_equity=10_000, equity=10_000)
    bars = _bars([(158.0, 158.0, 158.0, 158.0)], start="2026-10-26 07:00", session="2026-10-26")
    step = advance(acc, bars, ZERO, INST, daily_loss_limit=0.04, swap_long=0.01)
    assert step.account.cash == pytest.approx(10_000 + 100 * 0.01 * 3)  # 金曜 → 月曜で3日分


def test_hourly_breakout_target():
    rows = [{"high": 158.2, "low": 157.8, "close": 158.0}] * 20
    up = pd.DataFrame(rows + [{"high": 158.4, "low": 158.0, "close": 158.3}])
    flat = pd.DataFrame(rows + [{"high": 158.1, "low": 157.9, "close": 158.0}])
    assert hourly_breakout_target(up) == 1 and hourly_breakout_target(flat) == 0
    assert hourly_breakout_target(up.iloc[:5]) == 0


def test_account_round_trips_through_dict():
    acc = PaperAccount("ai", 10_000, 9_900, PaperPosition("EUR_JPY", -200, 170.0, 170.5, None, "x"), equity=9_950)
    assert PaperAccount.from_dict(acc.to_dict()) == acc


def test_moves_after_reports_change_and_extremes():
    from jqbt.application.fx_paper import moves_after

    index = pd.date_range("2026-10-21 10:00", periods=120, freq="1min")
    bars = pd.DataFrame({"open": 158.0, "high": 158.0, "low": 158.0, "close": 158.0}, index=index)
    bars.loc[index[30], "high"] = 158.25  # 30分後に +25 pips の上ひげ
    bars.loc[index[59], "close"] = 157.9
    moves = moves_after(bars, index[0], hours=(1, 4))
    assert moves == {"change_1h": -10.0, "up_1h": 25.0, "down_1h": 0.0}  # 4時間後はデータが無いので含めない
