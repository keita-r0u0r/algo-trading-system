"""GMOコイン 外国為替FX の **発注** クライアント。照会用（``gmo_fx.GmoFxClient``）とは分けてある。

  * 注文用のキー（Secret Manager の ``gmo-fx-trade-api-key`` / ``gmo-fx-trade-api-secret``）でしか作らない。
    照会用のキーは発注の権限を持たないので、取り違えても注文は通らない
  * 注文用のキーには IP の制限を付ける（クラウドの出口の IP を固定してある。infra/main.tf）
  * POST は1秒に1回まで。続けて出すときは間を空ける
  * 安全装置（数量の上限・1日の損失の上限・キルスイッチ）は、ここではなく ``application.fx_live_trade`` にある。
    このクライアントは言われた注文を出すだけ

公式の説明: https://api.coin.z.com/fxdocs/
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .gmo_fx import PRIVATE_URL, GmoFxClient, sign

#: POST の間隔（秒）。上限は1秒に1回
POST_INTERVAL = 1.1


class GmoFxTrader(GmoFxClient):
    def __init__(self, api_key: str, api_secret: str, client: httpx.Client | None = None, sleep=time.sleep) -> None:
        super().__init__(api_key, api_secret, client)
        self._sleep = sleep
        self._last_post = 0.0

    def __repr__(self) -> str:
        return "GmoFxTrader()"

    def _private_post(self, path: str, body: dict[str, Any]) -> Any:
        wait = POST_INTERVAL - (time.monotonic() - self._last_post)
        if wait > 0:
            self._sleep(wait)
        text = json.dumps(body, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        headers = {
            "API-KEY": self._key,
            "API-TIMESTAMP": timestamp,
            "API-SIGN": sign(self._secret, timestamp, "POST", path, text),
            "Content-Type": "application/json",
        }
        try:
            return self._unwrap(self._http.post(f"{PRIVATE_URL}{path}", content=text, headers=headers))
        finally:
            self._last_post = time.monotonic()

    @staticmethod
    def _first(data: Any) -> dict[str, Any]:
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}

    # --- 注文 ---------------------------------------------------------------
    def market_open(self, symbol: str, side: str, size: int, client_order_id: str) -> dict[str, Any]:
        """成行で新規に建てる。"""
        return self._first(self._private_post("/v1/order", {
            "symbol": symbol, "side": side, "size": str(int(size)),
            "executionType": "MARKET", "clientOrderId": client_order_id,
        }))

    def protect(self, symbol: str, position_side: str, position_id: int, size: int, stop_price: float,
                take_price: float | None, client_order_id: str) -> dict[str, Any]:
        """建玉に損切り（逆指値）と、あれば利確（指値）を置く。両方なら OCO。決済なので、売買は建玉の反対。"""
        side = "SELL" if position_side == "BUY" else "BUY"
        body: dict[str, Any] = {
            "symbol": symbol, "side": side, "clientOrderId": client_order_id,
            "settlePosition": [{"positionId": int(position_id), "size": str(int(size))}],
            "stopPrice": f"{stop_price:.3f}",
        }
        if take_price is not None:
            body.update({"executionType": "OCO", "limitPrice": f"{take_price:.3f}"})
        else:
            body["executionType"] = "STOP"
        return self._first(self._private_post("/v1/closeOrder", body))

    def market_close(self, symbol: str, position_side: str, position_id: int, size: int,
                     client_order_id: str) -> dict[str, Any]:
        """建玉を成行で決済する。"""
        side = "SELL" if position_side == "BUY" else "BUY"
        return self._first(self._private_post("/v1/closeOrder", {
            "symbol": symbol, "side": side, "executionType": "MARKET", "clientOrderId": client_order_id,
            "settlePosition": [{"positionId": int(position_id), "size": str(int(size))}],
        }))

    def cancel(self, root_order_ids: list[int]) -> Any:
        if not root_order_ids:
            return None
        return self._private_post("/v1/cancelOrders", {"rootOrderIds": [int(x) for x in root_order_ids][:10]})

    def executions_of(self, order_id: int) -> list[dict[str, Any]]:
        """注文の約定（建玉IDと約定の値段を知るため）。"""
        data = self._private_get("/v1/executions", {"orderId": str(order_id)})
        return list(data.get("list", [])) if isinstance(data, dict) else list(data or [])
