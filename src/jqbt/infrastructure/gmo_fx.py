"""GMOコイン 外国為替FX の API クライアント（**読み取りだけ**）。

公式の説明: https://api.coin.z.com/fxdocs/

## 注文はできない

このクライアントには GET しかない。注文・決済・取消（POST）は作っていない。
APIキーも「照会」の権限だけで発行する（漏れても注文・出金されない）。

## 認証（Private API）

ヘッダーに ``API-KEY`` / ``API-TIMESTAMP``（ミリ秒）/ ``API-SIGN`` を載せる。
署名は「時刻 + メソッド + パス + 本文」を、シークレットで HMAC-SHA256 した16進。
パスは ``/v1/...`` から（``/private`` は含めない）。GET の本文は空。

## レート制限

Private の GET は1秒に6回まで（口座ごと。照会用と発注用のキーで分け合う）。
続けて呼ぶと超えるので、``GET_INTERVAL`` 秒ずつ間を空ける。超えたら（ERR-5003）少し待って1回だけやり直す。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx

PUBLIC_URL = "https://forex-api.coin.z.com/public"
PRIVATE_URL = "https://forex-api.coin.z.com/private"
#: Private の GET の間隔（秒）。上限は1秒に6回だが、別のキーと分け合うので余裕を持つ
GET_INTERVAL = 0.4


class GmoError(RuntimeError):
    """API がエラーを返した。メッセージにキーやシークレットは含めない。"""


def sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """Private API の署名。"""
    message = f"{timestamp}{method}{path}{body}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class GmoFxClient:
    def __init__(self, api_key: str = "", api_secret: str = "", client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._secret = api_secret
        self._http = client or httpx.Client(timeout=10)

    def __repr__(self) -> str:
        return f"GmoFxClient(private={'有' if self._key else '無'})"

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != 0:
            messages = "; ".join(f"{m.get('message_code')}: {m.get('message_string')}" for m in payload.get("messages", []))
            raise GmoError(messages or f"status={payload.get('status')}")
        return payload["data"]

    # --- Public（キー不要） ----------------------------------------------
    def _public(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self._unwrap(self._http.get(f"{PUBLIC_URL}{path}", params=params))

    def status(self) -> str:
        """OPEN / CLOSE / MAINTENANCE"""
        return self._public("/v1/status")["status"]

    def symbols(self) -> list[dict[str, Any]]:
        """通貨ペアごとの最小・最大の注文数量、数量の刻み、価格の刻み。"""
        return self._public("/v1/symbols")

    def ticker(self) -> list[dict[str, Any]]:
        return self._public("/v1/ticker")

    def klines(self, symbol: str, interval: str, date: str, price_type: str = "BID") -> list[dict[str, Any]]:
        """足。date は1時間足まで YYYYMMDD（2023-10-28 以降）、4時間足から YYYY。"""
        return self._public(
            "/v1/klines", {"symbol": symbol, "priceType": price_type, "interval": interval, "date": date}
        )

    # --- Private（照会だけ） -----------------------------------------------
    def _private_get(self, path: str, params: dict[str, str] | None = None, retry: bool = True) -> Any:
        if not (self._key and self._secret):
            raise GmoError("APIキーとシークレットがありません")
        wait = GET_INTERVAL - (time.monotonic() - getattr(self, "_last_get", 0.0))
        if wait > 0:
            time.sleep(wait)
        timestamp = str(int(time.time() * 1000))
        headers = {
            "API-KEY": self._key,
            "API-TIMESTAMP": timestamp,
            "API-SIGN": sign(self._secret, timestamp, "GET", path),
        }
        try:
            return self._unwrap(self._http.get(f"{PRIVATE_URL}{path}", params=params, headers=headers))
        except GmoError as exc:
            if retry and "ERR-5003" in str(exc):  # 呼びすぎ。少し待って1回だけやり直す
                time.sleep(2)
                return self._private_get(path, params, retry=False)
            raise
        finally:
            self._last_get = time.monotonic()

    def assets(self) -> dict[str, Any] | list[dict[str, Any]]:
        """資産（評価額・証拠金・証拠金維持率など）。"""
        return self._private_get("/v1/account/assets")

    def latest_executions(self, symbol: str, count: int = 100) -> list[dict[str, Any]]:
        """直近1日の約定（最大100件、新しい順）。手数料・確定した損益・スワップを含む。"""
        data = self._private_get("/v1/latestExecutions", {"symbol": symbol, "count": str(count)})
        return list(data.get("list", [])) if isinstance(data, dict) else list(data or [])

    def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """今持っている建玉（最大100件）。"""
        params = {"symbol": symbol} if symbol else None
        data = self._private_get("/v1/openPositions", params)
        return list(data.get("list", [])) if isinstance(data, dict) else list(data or [])

    def position_summary(self) -> list[dict[str, Any]]:
        """通貨ペア・売り買いごとの建玉の合計と、評価損益。"""
        data = self._private_get("/v1/positionSummary")
        return list(data.get("list", [])) if isinstance(data, dict) else list(data or [])

