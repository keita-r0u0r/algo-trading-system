"""GMOコイン 外国為替FX クライアント（読み取りだけ）。通信はしない。"""

from __future__ import annotations

import hashlib
import hmac
import inspect

import httpx
import pytest

from jqbt.infrastructure.gmo_fx import GmoError, GmoFxClient, sign


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sign_is_hmac_of_timestamp_method_path_body():
    expected = hmac.new(b"s", b"1700000000000GET/v1/account/assets", hashlib.sha256).hexdigest()
    assert sign("s", "1700000000000", "GET", "/v1/account/assets") == expected


def test_private_request_is_signed_with_v1_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"status": 0, "data": [{"equity": "10000"}]})

    api = GmoFxClient("k", "s", client=_client(handler))
    assert api.assets() == [{"equity": "10000"}]
    assert seen["url"] == "https://forex-api.coin.z.com/private/v1/account/assets"
    assert seen["api-key"] == "k"
    assert seen["api-sign"] == sign("s", seen["api-timestamp"], "GET", "/v1/account/assets")


def test_api_error_is_raised_without_secrets():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 1, "messages": [{"message_code": "ERR-5201", "message_string": "MAINTENANCE"}]})

    with pytest.raises(GmoError, match="ERR-5201") as err:
        GmoFxClient("key-value", "secret-value", client=_client(handler)).assets()
    assert "key-value" not in str(err.value) and "secret-value" not in str(err.value)
    assert "key-value" not in repr(GmoFxClient("key-value", "secret-value"))


def test_private_needs_keys():
    with pytest.raises(GmoError):
        GmoFxClient(client=_client(lambda r: httpx.Response(500))).assets()


def test_client_cannot_place_orders():
    """注文・決済・取消の口は作らない（GET だけ）。"""
    source = inspect.getsource(GmoFxClient)
    assert ".post(" not in source and '"POST"' not in source
