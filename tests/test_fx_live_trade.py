"""自動売買（実際のお金）の安全装置と発注。通信はしない（偽物の API に差し替える）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from jqbt.application.fx_live_trade import (
    Close,
    LiveConfig,
    LivePosition,
    Open,
    Skip,
    book_from_events,
    client_id,
    gone_positions,
    plan,
    stop_take_prices,
)
from jqbt.infrastructure.gmo_fx import GmoError, sign
from jqbt.infrastructure.gmo_fx_trader import GmoFxTrader

ON = LiveConfig(enabled=True)
OPEN_AI = {"kind": "open", "account": "ai", "symbol": "USD_JPY", "units": 250, "price": 158.0,
           "stop": 157.75, "take": 158.5, "at": "2026-11-02T10:15:00+09:00"}


def run_plan(events, book=None, config=ON, **kw):
    base = dict(killed=False, equity=10_000.0, day_start_equity=10_000.0, available=10_000.0,
                prices={"USD_JPY": 158.0})
    base.update(kw)
    return plan(events, book or {}, config, **base)


# --- 計画と安全装置 ------------------------------------------------------------
def test_open_is_capped_to_max_units_and_keeps_stop_and_take_distance():
    [action] = run_plan([OPEN_AI])
    assert isinstance(action, Open)
    assert action.size == 100  # ペーパーの250通貨を100通貨に切る
    assert action.side == "BUY" and action.stop_pips == 25.0 and action.take_pips == 50.0


def test_nothing_is_ordered_unless_enabled():
    [action] = run_plan([OPEN_AI], config=LiveConfig(enabled=False))
    assert isinstance(action, Skip) and "有効" in action.reason


@pytest.mark.parametrize("kw, words", [
    ({"killed": True}, "キルスイッチ"),
    ({"equity": 9_400.0, "day_start_equity": 10_000.0}, "今日の損失"),
    ({"available": 500.0}, "取引余力"),
])
def test_safety_stops_new_positions(kw, words):
    [action] = run_plan([OPEN_AI], **kw)
    assert isinstance(action, Skip) and words in action.reason


def test_one_position_per_account():
    book = {"ai": LivePosition("ai", "USD_JPY", "BUY", 100, 1)}
    [action] = run_plan([OPEN_AI], book)
    assert isinstance(action, Skip) and "持っている" in action.reason


def test_accounts_not_mirrored_are_ignored():
    event = dict(OPEN_AI, account="rule_hourly_breakout")
    assert run_plan([event]) == []


def test_decision_close_closes_live_position_but_stop_hits_are_left_to_gmo():
    book = {"ai": LivePosition("ai", "USD_JPY", "BUY", 100, 77, [5])}
    [close] = run_plan([{"kind": "close", "account": "ai", "reason": "AI: 閉じる", "at": "2026-11-02T11:00"}], book)
    assert isinstance(close, Close) and close.position_id == 77 and close.protect_root_ids == (5,)
    assert run_plan([{"kind": "close", "account": "ai", "reason": "損切り", "at": "x"}], book) == []


def test_closing_is_allowed_even_when_killed():
    book = {"ai": LivePosition("ai", "USD_JPY", "BUY", 100, 77)}
    [close] = run_plan([{"kind": "close", "account": "ai", "reason": "判断", "at": "x"}], book, killed=True)
    assert isinstance(close, Close)


def test_book_and_gone_positions():
    events = [
        {"kind": "live_open", "account": "ai", "symbol": "USD_JPY", "side": "BUY", "size": 100, "position_id": 1},
        {"kind": "live_protect", "account": "ai", "position_id": 1, "root_order_id": 9},
        {"kind": "live_open", "account": "carry_trend_USD_JPY", "symbol": "USD_JPY", "side": "BUY", "size": 100,
         "position_id": 2},
        {"kind": "live_close", "account": "carry_trend_USD_JPY", "position_id": 2},
    ]
    book = book_from_events(events)
    assert list(book) == ["ai"] and book["ai"].protect_root_ids == [9]
    assert [p.position_id for p in gone_positions(book, open_position_ids=set())] == [1]


def test_stop_take_prices_and_client_id():
    assert stop_take_prices("BUY", 158.0, 25, 50) == (157.75, 158.5)
    assert stop_take_prices("SELL", 158.0, 25, None) == (158.25, None)
    cid = client_id("carry_trend_USD_JPY", "2026-11-02T10:15:00+09:00", "o")
    assert len(cid) <= 36 and cid.isalnum()
    assert cid != client_id("carry_trend_USD_JPY", "2026-11-02T10:30:00+09:00", "o")


# --- 発注のクライアント ------------------------------------------------------------
def test_trader_signs_post_body_and_sends_market_order():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = request.url.path, request.content.decode()
        seen["sign"], seen["ts"] = request.headers["api-sign"], request.headers["api-timestamp"]
        return httpx.Response(200, json={"status": 0, "data": [{"orderId": 11, "rootOrderId": 11}]})

    trader = GmoFxTrader("k", "s", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    result = trader.market_open("USD_JPY", "BUY", 100, "oai202611021015")
    assert result["orderId"] == 11 and seen["path"] == "/private/v1/order"
    body = json.loads(seen["body"])
    assert body == {"symbol": "USD_JPY", "side": "BUY", "size": "100", "executionType": "MARKET",
                    "clientOrderId": "oai202611021015"}
    assert seen["sign"] == sign("s", seen["ts"], "POST", "/v1/order", seen["body"])


def test_protect_uses_oco_with_take_and_stop_otherwise():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"status": 0, "data": [{"rootOrderId": 5}]})

    trader = GmoFxTrader("k", "s", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    trader.protect("USD_JPY", "BUY", 77, 100, 157.75, 158.5, "p1")
    trader.protect("USD_JPY", "BUY", 77, 100, 157.75, None, "p2")
    assert bodies[0]["executionType"] == "OCO" and bodies[0]["side"] == "SELL" and bodies[0]["limitPrice"] == "158.500"
    assert bodies[1]["executionType"] == "STOP" and "limitPrice" not in bodies[1]
    assert bodies[0]["settlePosition"] == [{"positionId": 77, "size": "100"}]


# --- 安全装置: 損切りを置けなければ、決済してキルスイッチ ---------------------------------
def test_failsafe_closes_and_kills_when_stop_cannot_be_placed(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import fx_live_trade

    monkeypatch.setattr(fx_live_trade, "KILL", tmp_path / "KILL_fx_live")
    monkeypatch.setattr(fx_live_trade.time, "sleep", lambda s: None)
    calls = []

    class Fake:
        def market_open(self, *a):
            calls.append("open")
            return {"orderId": 1}

        def executions_of(self, order_id):
            return [{"positionId": 77, "price": "158.00"}]

        def protect(self, *a):
            calls.append("protect")
            raise GmoError("ERR-XXXX")

        def market_close(self, *a):
            calls.append("close")
            return {"orderId": 2}

    action = Open("ai", "USD_JPY", "BUY", 100, 25.0, None, "oai1")
    events = fx_live_trade.execute(Fake(), action, "2026-11-02T10:15:00+09:00")
    assert calls == ["open", "protect", "protect", "protect", "close"]
    assert (tmp_path / "KILL_fx_live").exists()
    assert [e["kind"] for e in events][-2:] == ["live_close", "live_error"]
