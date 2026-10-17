"""画面に出す値。秘密を出さないこと、制限の変更の向き、データの鮮度を固定する。"""

from __future__ import annotations

import datetime as dt

from jqbt.core.gates import Check, Mode
from jqbt.core.risk import RiskLimits
from jqbt.interfaces import viewmodels as vm


def test_secrets_are_shown_only_as_set_or_unset():
    assert vm.secret_state("abc-very-secret") == "設定済み"
    assert vm.secret_state("") == "未設定"
    assert vm.secret_state(None) == "未設定"


def test_freshness_flags_data_too_old_for_live_signals():
    fresh = vm.freshness(dt.date(2026, 7, 14), dt.date(2026, 7, 16))
    stale = vm.freshness(dt.date(2026, 7, 16), dt.date(2026, 10, 17))
    assert fresh["ok"] and not stale["ok"]
    assert "古すぎる" in stale["message"] and stale["age_days"] == 93


def test_unimplemented_modes_are_listed_but_not_available():
    options = {o["mode"]: o for o in vm.mode_options()}
    assert options[Mode.PAPER]["available"]
    assert not options[Mode.AUTO]["available"] and "未実装" in options[Mode.AUTO]["note"]


def test_limit_change_tells_whether_it_applies_now_or_later():
    base = RiskLimits(max_order_value=5_000)
    tighter = vm.limit_change(base, RiskLimits(max_order_value=3_000))
    looser = vm.limit_change(base, RiskLimits(max_order_value=9_000))
    same = vm.limit_change(base, base)

    assert not tighter["loosening"] and "すぐ" in tighter["timing"]
    assert looser["loosening"] and "24時間後" in looser["timing"]
    assert not same["changed"]
    assert {r["向き"] for r in looser["rows"]} == {"緩める", "—"}


def test_status_cards_summarize_the_account():
    status = {
        "mode": Mode.PAPER, "strategy": "regime_hold", "equity": 10_385.4, "cash": 2_961.0,
        "positions": [{"symbol": "9432", "quantity": 13, "price": 153.0, "value": 1_989.0}],
        "gates": [Check("継続日数", True, ""), Check("約定数", False, "")], "gates_passed": False,
    }
    cards = {c["label"]: c for c in vm.status_cards(status)}
    assert cards["評価額"]["value"] == "10,385円"
    assert cards["増額の条件"]["value"] == "未達" and cards["増額の条件"]["note"] == "1 / 2"


def test_fill_rows_show_adverse_slippage_as_positive():
    rows = vm.fill_rows([
        {"date": "2026-07-01", "side": "buy", "symbol": "A", "quantity": 1, "price": 101.0, "fee": 0, "ref_open": 100.0, "source": "manual"},
        {"date": "2026-07-02", "side": "sell", "symbol": "A", "quantity": 1, "price": 99.0, "fee": 0, "ref_open": 100.0, "source": "manual"},
        {"date": "2026-07-03", "side": "buy", "symbol": "B", "quantity": 1, "price": 50.0, "fee": 0, "ref_open": None, "source": "manual"},
    ])
    assert rows[0]["始値とのずれ(%)"] == 1.0
    assert rows[1]["始値とのずれ(%)"] == 1.0
    assert rows[2]["始値とのずれ(%)"] is None
