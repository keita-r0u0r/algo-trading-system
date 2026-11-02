"""FX の実際の約定の記録（読むだけ）。通信はしない。"""

from __future__ import annotations

import httpx
import pytest

from jqbt.application.fx_live import new_executions, slippage_pips, summarize
from jqbt.infrastructure.gmo_fx import GmoFxClient


def test_new_executions_skip_known_and_sort_oldest_first():
    fetched = [{"executionId": 3, "timestamp": "2026-11-02T03:00:00Z"},
               {"executionId": 1, "timestamp": "2026-11-02T01:00:00Z"},
               {"executionId": 2, "timestamp": "2026-11-02T02:00:00Z"}]
    assert [e["executionId"] for e in new_executions(fetched, {"2"})] == [1, 3]


def test_slippage_is_positive_when_worse():
    assert slippage_pips("BUY", 158.003, 157.990, 158.000) == pytest.approx(0.3)  # 買値より 0.3 pips 高く買った
    assert slippage_pips("SELL", 157.985, 157.990, 158.000) == pytest.approx(0.5)  # 売値より 0.5 pips 安く売った
    assert slippage_pips("BUY", 157.999, 157.990, 158.000) == pytest.approx(-0.1)  # 有利なずれ


def test_summarize_totals():
    rows = [{"settleType": "OPEN", "fee": "0.3", "slippage_pips": 0.2},
            {"settleType": "CLOSE", "fee": "0.3", "lossGain": "120", "settledSwap": "5", "slippage_pips": 0.6}]
    s = summarize(rows)
    assert s == {"executions": 2, "closed": 1, "loss_gain": 120.0, "fee": 0.6, "swap": 5.0,
                 "slippage_avg": 0.4, "slippage_worst": 0.6}


def test_client_reads_executions_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/private/v1/latestExecutions" and request.method == "GET"
        return httpx.Response(200, json={"status": 0, "data": {"list": [{"executionId": 1}]}})

    api = GmoFxClient("k", "s", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.latest_executions("USD_JPY") == [{"executionId": 1}]
