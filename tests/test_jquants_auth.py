"""V2 の認証とレスポンス形式の検証。

V2 で破壊的に変わったのは次の4点で、どれも **例外にならず静かに失敗する**。

  1. 認証: refresh token / id token の2段方式は廃止 → ``x-api-key`` ヘッダー
  2. レスポンス: トップレベルのキーがエンドポイントごとの名前 → 一律 ``data``
  3. パス: ``/markets/trading_calendar`` → ``/markets/calendar``
  4. レート制限: Free は5回/分（V1 には明示がなかった）

2 を間違えると ``payload.get(key, [])`` が空リストを返すだけなので、
「その日はデータがなかった」として取り込みが素通りする。
APIキーを持たずに検証できる唯一の場所なので、ここで固定する。
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from jqbt.infrastructure.jquants import (
    API_KEY_ENV,
    DATA_KEY,
    RATE_LIMIT_BACKOFF,
    RATE_LIMITS,
    JQuantsClient,
    JQuantsError,
    _RateLimiter,
    estimate_fetch_minutes,
)


def recorder(payloads: list[dict]):
    """リクエストを記録し、用意した応答を順に返すトランスポート。"""
    seen: list[httpx.Request] = []
    remaining = list(payloads)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=remaining.pop(0))

    return httpx.MockTransport(handle), seen


def client(payloads: list[dict], **kwargs) -> tuple[JQuantsClient, list]:
    transport, seen = recorder(payloads)
    #: premium を指定して待ち時間を最小にする（レート制限は別途検証する）
    return (
        JQuantsClient(api_key="k-test", transport=transport, plan="premium", **kwargs),
        seen,
    )


# ----------------------------------------------------------------------
# 認証
# ----------------------------------------------------------------------
def test_api_key_goes_in_the_x_api_key_header():
    """``Authorization: Bearer`` ではなく ``x-api-key``。"""
    api, seen = client([{DATA_KEY: []}])
    api.listed_master(dt.date(2024, 1, 4))

    assert seen[0].headers["x-api-key"] == "k-test"
    assert "authorization" not in seen[0].headers


def test_no_token_exchange_request_is_made():
    """認証用の事前リクエストが発生しないこと。

    V1 は毎回 ``/token/auth_refresh`` を叩いていた。V2 でそれは404になる。
    """
    api, seen = client([{DATA_KEY: []}])
    api.listed_master(dt.date(2024, 1, 4))

    assert len(seen) == 1
    assert "token" not in str(seen[0].url)


def test_missing_api_key_is_reported_before_any_request(monkeypatch):
    #: 環境変数が残っていると「未設定」の検証にならないので明示的に消す
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    api = JQuantsClient(api_key=None)
    assert not api.is_available
    with pytest.raises(JQuantsError, match=API_KEY_ENV):
        api.listed_master(dt.date(2024, 1, 4))


def test_unauthorised_response_names_the_api_key():
    """401/403 を「サーバーエラー」として再試行しないこと。

    キーが違うだけなら、5回粘っても結果は変わらない。
    """

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    api = JQuantsClient(api_key="bad", transport=httpx.MockTransport(handle))
    with pytest.raises(JQuantsError, match=API_KEY_ENV):
        api.listed_master(dt.date(2024, 1, 4))


# ----------------------------------------------------------------------
# レスポンス形式
# ----------------------------------------------------------------------
def test_rows_are_read_from_the_data_key():
    api, _ = client(
        [{DATA_KEY: [{"Code": "72030", "Date": "2024-01-04", "C": "2500", "AdjC": "2500"}]}]
    )
    frame = api.daily_bars(date=dt.date(2024, 1, 4))

    assert len(frame) == 1
    assert frame["symbol"].iloc[0] == "7203"


def test_old_v1_top_level_keys_are_not_used():
    """V1 のキー名で返しても、1件も読まないこと。

    ここが通らない（＝読めてしまう）なら、まだ V1 のキーを見ている。
    """
    api, _ = client([{"daily_quotes": [{"Code": "72030", "Date": "2024-01-04"}]}])
    assert api.daily_bars(date=dt.date(2024, 1, 4)).empty


def test_pagination_key_is_followed():
    api, seen = client(
        [
            {DATA_KEY: [{"Code": "72030", "Date": "2024-01-04"}], "pagination_key": "p2"},
            {DATA_KEY: [{"Code": "99840", "Date": "2024-01-04"}]},
        ]
    )
    frame = api.listed_master(dt.date(2024, 1, 4))

    assert len(frame) == 2
    assert "pagination_key=p2" in str(seen[1].url)


# ----------------------------------------------------------------------
# エンドポイントのパス
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "call, expected",
    [
        (lambda a: a.listed_master(dt.date(2024, 1, 4)), "/v2/equities/master"),
        (lambda a: a.daily_bars(date=dt.date(2024, 1, 4)), "/v2/equities/bars/daily"),
        (lambda a: a.statements(code="7203"), "/v2/fins/summary"),
        (
            lambda a: a.trading_calendar(dt.date(2024, 1, 1), dt.date(2024, 1, 31)),
            "/v2/markets/calendar",
        ),
    ],
)
def test_v2_endpoint_paths(call, expected):
    """V1 のパスは V2 に存在しない（404 になる）。"""
    api, seen = client([{DATA_KEY: []}])
    call(api)
    assert seen[0].url.path == expected


# ----------------------------------------------------------------------
# レート制限
# ----------------------------------------------------------------------
def test_free_plan_interval_has_a_safety_margin():
    """5回/分 = 公称12秒間隔。ぎりぎりだと窓を超えて 429 になるので余裕を持たせる。

    429 を受けてからバックオフする作りだと、上限ぎりぎりで
    待ち時間が伸び続けるので、投げる前に間隔を空ける。
    """
    assert RATE_LIMITS["free"] == 5
    assert _RateLimiter(RATE_LIMITS["free"]).min_interval == pytest.approx(21.6)


def test_rate_limiter_sleeps_between_calls(monkeypatch):
    slept: list[float] = []
    #: 1回目で _last=0.0 を記録 → 2回目の経過判定で 1.0（＝1秒経過）
    clock = iter([0.0, 1.0, 1.0])
    monkeypatch.setattr("jqbt.infrastructure.jquants.time.sleep", slept.append)
    monkeypatch.setattr(
        "jqbt.infrastructure.jquants.time.monotonic", lambda: next(clock)
    )

    limiter = _RateLimiter(5)
    limiter.wait()  # 1回目は待たない
    limiter.wait()  # 1秒しか経っていないので 20.6 秒待つ

    assert slept == [pytest.approx(20.6)]


def flaky_client(steps: list):
    """steps の要素を順に実行する。Exception は送出し、httpx.Response は返す。"""
    remaining = list(steps)

    def handle(request: httpx.Request) -> httpx.Response:
        step = remaining.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    return JQuantsClient(
        api_key="k-test", transport=httpx.MockTransport(handle), plan="premium"
    )


def test_429_waits_for_retry_after(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("jqbt.infrastructure.jquants.time.sleep", slept.append)
    api = flaky_client(
        [
            httpx.Response(429, headers={"Retry-After": "30"}),
            httpx.Response(200, json={DATA_KEY: []}),
        ]
    )
    api.listed_master(dt.date(2024, 1, 4))

    #: premium のレートリミッターが挟む短い待ちは除く
    assert [s for s in slept if s >= 1] == [30.0]


def test_429_without_retry_after_waits_a_full_minute(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("jqbt.infrastructure.jquants.time.sleep", slept.append)
    api = flaky_client(
        [httpx.Response(429), httpx.Response(200, json={DATA_KEY: []})]
    )
    api.listed_master(dt.date(2024, 1, 4))

    assert [s for s in slept if s >= 1] == [RATE_LIMIT_BACKOFF]


def test_read_timeout_is_retried(monkeypatch):
    monkeypatch.setattr("jqbt.infrastructure.jquants.time.sleep", lambda s: None)
    api = flaky_client(
        [httpx.ReadTimeout("timed out"), httpx.Response(200, json={DATA_KEY: []})]
    )

    assert api.listed_master(dt.date(2024, 1, 4)).empty


def test_repeated_timeouts_end_in_jquants_error(monkeypatch):
    monkeypatch.setattr("jqbt.infrastructure.jquants.time.sleep", lambda s: None)
    api = flaky_client([httpx.ReadTimeout("timed out")] * 6)

    with pytest.raises(JQuantsError, match="繰り返し失敗"):
        api.listed_master(dt.date(2024, 1, 4))


def test_unknown_plan_is_rejected():
    with pytest.raises(JQuantsError, match="未知のプラン"):
        JQuantsClient(api_key="k", plan="unlimited")


def test_fetch_time_estimate_is_honest_about_the_free_plan():
    """2年分（約490営業日）が Free では100分近くかかること。

    「待てる時間なのか」を取り込み前に示すために出す。
    """
    assert estimate_fetch_minutes(490, "free") == pytest.approx(98.0)
    assert estimate_fetch_minutes(490, "light") == pytest.approx(8.17, abs=0.01)
