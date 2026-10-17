"""運用の流れ: 凍結、古いデータの拒否、約定の記録、制限の非対称な変更、ペーパーの再生。

資金が動く手前の仕組みなので、「やってはいけないこと」が確実に拒否されることを固定する。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from jqbt.application.operations import OperationsError, OperationsService, StaleDataError
from jqbt.core.gates import Mode
from jqbt.core.risk import RiskLimits
from jqbt.infrastructure.ledger import Ledger
from jqbt.strategies import RegimeHold

PARAMS = {"ma": 5, "rebalance_every": 3, "max_positions": 2, "min_turnover": 0}


def factory(params):
    return RegimeHold(**params)


def make_panel(days: int = 40) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-01", periods=days)
    frames = []
    for i, symbol in enumerate(["A", "B", "C", "D"]):
        price = 100.0 * (1.01 + i * 0.001) ** pd.RangeIndex(days).to_numpy() * (1 + i)
        frames.append(
            pd.DataFrame(
                {
                    "date": index, "symbol": symbol, "open": price, "high": price, "low": price,
                    "close": price, "raw_close": price, "volume": 1e6, "turnover": 1e9 * (4 - i),
                }
            )
        )
    return pd.concat(frames).set_index(["date", "symbol"])


@pytest.fixture
def service(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    svc = OperationsService(ledger, make_panel(), factory, lot_size=1)
    yield svc
    ledger.close()


def init(svc, mode=Mode.SIGNAL):
    svc.init(
        cash=10_000.0, strategy="regime_hold", params=PARAMS, version="abc123",
        limits=RiskLimits(max_order_value=5_000, max_position_share=0.6, max_positions=2),
        mode=mode, started_on=dt.date(2024, 1, 1), at="2024-01-01T00:00:00",
    )


DAYS = make_panel().index.get_level_values("date").unique()
LAST = DAYS[-1].date()


def test_the_strategy_is_frozen_after_init(service):
    init(service)
    with pytest.raises(OperationsError, match="初期化済み"):
        init(service)


def test_stale_data_is_refused_for_live_signals(service):
    init(service, Mode.SIGNAL)
    with pytest.raises(StaleDataError, match="古すぎ"):
        service.plan(LAST, today=LAST + dt.timedelta(days=84))


def test_signals_are_produced_when_the_data_is_fresh(service):
    init(service, Mode.SIGNAL)
    result = service.plan(LAST, today=LAST + dt.timedelta(days=1))
    assert result.signals
    assert all(s["status"] == "planned" and s["side"] == "buy" for s in result.signals)
    assert len(result.signals) <= 2  # 同時保有の上限


def test_planning_twice_does_not_duplicate_signals(service):
    init(service, Mode.SIGNAL)
    first = service.plan(LAST, today=LAST)
    again = service.plan(LAST, today=LAST)
    assert again.already and len(service.ledger.signals()) == len(first.signals)


def test_a_stopped_system_produces_nothing(service):
    init(service, Mode.STOP)
    with pytest.raises(OperationsError, match="停止中"):
        service.plan(LAST, today=LAST)


def test_unimplemented_modes_are_refused(service):
    init(service)
    with pytest.raises(OperationsError, match="未実装"):
        service.set_mode(Mode.AUTO, reason="試す", at="x")


def test_record_fill_measures_slippage_against_the_open(service):
    init(service)
    day = DAYS[10].date()
    open_price = float(make_panel().xs("A", level="symbol")["open"].iloc[10])
    record = service.record_fill(day=day, symbol="A", side="buy", quantity=10, price=open_price * 1.004)

    assert record["ref_open"] == pytest.approx(open_price)
    stats = service.stats()
    assert stats.mean_slippage == pytest.approx(0.004, abs=1e-6)


def test_record_fill_rejects_impossible_trades(service):
    init(service)
    day = DAYS[10].date()
    with pytest.raises(OperationsError, match="保有"):
        service.record_fill(day=day, symbol="A", side="sell", quantity=1, price=100)
    with pytest.raises(OperationsError, match="現金"):
        service.record_fill(day=day, symbol="A", side="buy", quantity=10_000, price=100)
    with pytest.raises(OperationsError, match="正の値"):
        service.record_fill(day=day, symbol="A", side="buy", quantity=0, price=100)


def test_tightening_limits_is_immediate_and_loosening_waits_a_day(service):
    init(service)
    now = dt.datetime(2024, 3, 1, 9, 0)

    assert service.request_limits(RiskLimits(max_order_value=1_000, max_position_share=0.6, max_positions=2),
                                  reason="絞る", now=now) == "applied"
    assert service.limits.max_order_value == 1_000

    assert service.request_limits(RiskLimits(max_order_value=9_000, max_position_share=0.6, max_positions=2),
                                  reason="緩める", now=now) == "pending"
    assert service.limits.max_order_value == 1_000  # まだ変わらない

    assert not service.apply_pending(now + dt.timedelta(hours=23))
    assert service.apply_pending(now + dt.timedelta(hours=25))
    assert service.limits.max_order_value == 9_000


def test_every_setting_change_is_in_the_audit_log(service):
    init(service)
    service.set_mode(Mode.PAPER, reason="練習", at="2024-02-01T00:00:00")
    log = service.ledger.audit_log()
    assert log[0]["key"] == "mode" and log[0]["reason"] == "練習" and log[0]["old"] == "1" and log[0]["new"] == "2"


def test_paper_replay_runs_the_whole_flow_and_keeps_the_books_consistent(service):
    init(service, Mode.PAPER)
    outcome = service.replay(DAYS[0].date(), LAST)

    assert outcome["first_fill"] is not None
    fills = service.ledger.fills()
    assert fills and all(f["source"] == "paper" for f in fills)

    status = service.status(LAST)
    held = sum(p["value"] for p in status["positions"])
    assert status["cash"] + held == pytest.approx(status["equity"], rel=1e-9)
    assert service.stats().days == len(DAYS)
    assert status["cash"] >= -1e-6  # 現金が負にならない


def test_scale_up_gates_do_not_pass_after_a_short_run(service):
    init(service, Mode.PAPER)
    service.replay(DAYS[0].date(), LAST)
    assert not service.status(LAST)["gates_passed"]


def test_paper_fills_never_count_as_measured_slippage(service):
    """ペーパーは想定のずれをそのまま入れるので、増額の条件の根拠にしてはいけない。"""
    init(service, Mode.PAPER)
    service.replay(DAYS[0].date(), LAST)
    assert service.ledger.fills()
    stats = service.stats()
    assert stats.mean_slippage is None
    slippage = next(c for c in service.gates() if c.name == "スリッページ")
    assert not slippage.ok
