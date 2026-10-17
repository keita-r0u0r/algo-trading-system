"""J-Quants API クライアント（V2）。

## 認証（V1 から変わった点）

V2 で **トークン方式は廃止** された。V1 の ``/v1/token/auth_user`` と
``/v1/token/auth_refresh`` は削除され、ダッシュボードで発行した
API キーを ``x-api-key`` ヘッダーに載せる方式に統一されている。
キー自体に有効期限はなく、再発行・削除で管理する。

## バックテストの正しさに直結する3点

1. **銘柄一覧は date を指定して取る。**
   ``/equities/master`` は ``date`` を受け取り、その日時点で上場していた
   銘柄を返す。指定しないと「今日上場している銘柄」だけが返り、
   その期間に上場廃止になった銘柄が母集団から消える（生存者バイアス）。
   潰れた会社を除いた成績は、当然ながら実際より良く出る。

2. **調整後価格（AdjC 等）は遡及して再計算される。**
   分割が起きると過去の値が書き換わる。リターンの計算には調整後で正しいが、
   「株価1000円以下の銘柄」のような **価格水準の条件には使えない**。
   将来の分割を知っていることになるため。水準の判定には調整前（C）を使う。

3. **財務は DiscDate / DiscTime を使う。**
   決算期（CurPerEn）ではなく、TDnet で開示された日時。
   会計期間の終わりを基準にすると、まだ公表されていない数字で
   売買したことになる。

## レート制限

Free は **5回/分**。日付単位で2年分を取ると約490リクエストになり、
最短でも100分近くかかる。``_RateLimiter`` で投げる前に間隔を空ける。
429 を受けてから待つのではなく、**そもそも超えない速度で投げる**。
取得済みの日付はスキップする作りなので、途中で止めても再開できる。

## プランによる制約

Free は「直近12週間を除く2年間」。過去を再現する用途では遅延は問題にならない。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any, Iterator

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

BASE_URL = "https://api.jquants.com/v2"
API_KEY_ENV = "JQUANTS_API_KEY"

#: V2 は全エンドポイントがこのキーの配列でデータを返す
DATA_KEY = "data"

#: Free プランの遅延（日）
FREE_PLAN_DELAY_DAYS = 84

#: プランごとの上限（回/分）。超えると 429
RATE_LIMITS = {"free": 5, "light": 60, "standard": 120, "premium": 500}
DEFAULT_PLAN = "free"


class JQuantsError(RuntimeError):
    pass


#: 公称の上限ぎりぎりで投げると、時計のずれや通信の揺らぎで窓を超えて 429 になる。
#: 超過が続くと IP 単位で制限される恐れもあるので、間隔に余裕を持たせる
RATE_LIMIT_MARGIN = 1.8

#: 429 のあと、Retry-After が無いときに待つ秒数（1分窓のリセットを待つ）
RATE_LIMIT_BACKOFF = 60.0


class _RateLimiter:
    """前回の呼び出しから一定時間が経つまで待つ。

    Free の 5回/分 は公称12秒間隔。ここに ``RATE_LIMIT_MARGIN`` を掛けた間隔を空ける。
    429 を受けてから指数バックオフする作りだと、上限ぎりぎりで動かしたときに
    待ち時間が伸び続けて安定しない。
    """

    def __init__(self, per_minute: int, margin: float = RATE_LIMIT_MARGIN) -> None:
        self.min_interval = 60.0 / per_minute * margin if per_minute > 0 else 0.0
        self._last: float | None = None

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        if self._last is not None:
            remaining = self.min_interval - (time.monotonic() - self._last)
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()


def _retry_after(response: httpx.Response) -> float:
    """429 後に待つ秒数。Retry-After（秒）があれば従い、無ければ1分窓ぶん待つ。"""
    try:
        return min(max(float(response.headers["Retry-After"]), 1.0), 300.0)
    except (KeyError, ValueError):
        return RATE_LIMIT_BACKOFF


class JQuantsClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        plan: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        # テストで差し替えるための口。HTTP は Imperative Shell の責務なので、
        # 依存を外から渡せるようにしておく（本番は None で既定の実装）
        self._transport = transport
        self._api_key = api_key or os.getenv(API_KEY_ENV)
        plan_name = (plan or os.getenv("JQUANTS_PLAN") or DEFAULT_PLAN).lower()
        if plan_name not in RATE_LIMITS:
            raise JQuantsError(
                f"未知のプラン: {plan_name}（{'/'.join(RATE_LIMITS)} のいずれか）"
            )
        self.plan = plan_name
        self._limiter = _RateLimiter(RATE_LIMITS[plan_name])

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise JQuantsError(
                f"{API_KEY_ENV} が設定されていません。"
                "https://jpx-jquants.com/ のダッシュボード [API Keys] で発行してください"
            )
        return {"x-api-key": self._api_key}

    def _paged(self, path: str, params: dict[str, Any]) -> Iterator[dict]:
        """pagination_key を辿って全件返す。

        レート制限（429）とサーバーエラーは指数バックオフで数回だけ再試行する。
        無限に粘らないのは、夜間バッチが朝まで詰まるのを避けるため。
        """
        params = {k: v for k, v in params.items() if v is not None}
        attempt = 0
        while True:
            self._limiter.wait()
            try:
                with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                    response = client.get(
                        f"{self.base_url}{path}", params=params, headers=self._headers()
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                if attempt > 5:
                    raise JQuantsError(f"{path} への接続が繰り返し失敗しました: {exc!r}") from exc
                wait = min(2**attempt, 60)
                logger.warning("%s %s → %ds 待機して再試行", path, type(exc).__name__, wait)
                time.sleep(wait)
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                attempt += 1
                if attempt > 5:
                    raise JQuantsError(f"{path} が繰り返し失敗しました: {response.status_code}")
                if response.status_code == 429:
                    wait = _retry_after(response)
                else:
                    wait = min(2**attempt, 60)
                logger.warning("%s %s → %ds 待機して再試行", path, response.status_code, wait)
                time.sleep(wait)
                continue
            if response.status_code in (401, 403):
                raise JQuantsError(
                    f"{path}: 認証に失敗しました（{response.status_code}）。"
                    f"{API_KEY_ENV} が正しいか、プランで利用できるデータかを確認してください"
                )
            if response.status_code != 200:
                raise JQuantsError(f"{path}: {response.status_code} {response.text[:200]}")

            attempt = 0
            payload = response.json()
            yield from payload.get(DATA_KEY, [])
            token = payload.get("pagination_key")
            if not token:
                return
            params = {**params, "pagination_key": token}

    # ------------------------------------------------------------------
    def listed_master(self, date: dt.date | None = None) -> pd.DataFrame:
        """指定日時点の上場銘柄一覧。

        **date を省略しないこと。** 省略すると当日時点の一覧になり、
        その時点で既に上場廃止になっていた銘柄が落ちる。
        """
        params = {"date": date.strftime("%Y%m%d")} if date else {}
        rows = list(self._paged("/equities/master", params))
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        frame["symbol"] = frame["Code"].map(_normalize_code)
        return frame

    def daily_bars(
        self,
        code: str | None = None,
        date: dt.date | None = None,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> pd.DataFrame:
        """株価四本値。銘柄指定か日付指定のどちらかが必須。

        日付指定（date）のほうが、全銘柄を一括で取れるので取り込みは速い。
        Free の 5回/分 では、この差がそのまま所要時間の差になる。
        """
        params = {
            "code": code,
            "date": date.strftime("%Y%m%d") if date else None,
            "from": start.strftime("%Y%m%d") if start else None,
            "to": end.strftime("%Y%m%d") if end else None,
        }
        return _bars_frame(list(self._paged("/equities/bars/daily", params)))

    def statements(
        self, code: str | None = None, date: dt.date | None = None
    ) -> pd.DataFrame:
        """財務情報。``date`` は開示日であって決算期ではない。"""
        params = {
            "code": code,
            "date": date.strftime("%Y%m%d") if date else None,
        }
        return _statements_frame(list(self._paged("/fins/summary", params)))

    def trading_calendar(self, start: dt.date, end: dt.date) -> pd.DataFrame:
        """取引カレンダー。V2 のパスは ``/markets/calendar``。

        V1 の ``/markets/trading_calendar`` は存在しない。
        """
        rows = list(
            self._paged(
                "/markets/calendar",
                {"from": start.strftime("%Y%m%d"), "to": end.strftime("%Y%m%d")},
            )
        )
        return _calendar_frame(rows)


# ----------------------------------------------------------------------
def _normalize_code(code: Any) -> str:
    """J-Quants の5桁コードを4桁の内部表記に直す。

    "72030" → "7203"。末尾が0でない5桁（一部の優先株など）はそのまま。
    """
    text = str(code).strip()
    if len(text) == 5 and text.endswith("0"):
        return text[:-1]
    return text


#: 休日区分の列名。V2 は HolDiv、V1 は HolidayDivision だった
HOLIDAY_COLUMNS = ("HolDiv", "HolidayDivision")

#: 実績を伴う開示の DocType に共通して入る文字列
ACTUAL_DOC_MARKER = "FinancialStatements"


def _calendar_frame(rows: list[dict] | pd.DataFrame) -> pd.DataFrame:
    """取引カレンダーを ``date`` / ``is_open`` に整える。

    列名を取り違えると全日が営業日扱いになり、休場日にも約定したことになる。
    黙って ``True`` に倒さず、列が無ければ落とす。
    """
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["date", "is_open"])

    column = next((c for c in HOLIDAY_COLUMNS if c in frame.columns), None)
    if column is None:
        raise JQuantsError(
            f"取引カレンダーに休日区分の列がありません: {list(frame.columns)}"
        )
    return pd.DataFrame(
        {
            "date": pd.to_datetime(frame["Date"]),
            # "0"=非営業日。それ以外（半休を含む）は立会あり
            "is_open": frame[column].astype(str) != "0",
        }
    )


def _statements_frame(rows: list[dict]) -> pd.DataFrame:
    """財務情報を整える。

    ``/fins/summary`` は決算短信だけを返すわけではない。
    ``EarnForecastRevision``（業績予想の修正）が混ざっており、これは

      - **期中に** 開示される（CurPerEn より前の日付を持つ）
      - 売上・利益などの **実績値が空** で返る

    実績と混ぜると、期末前の日付で決算が出たことになり、
    空文字を 0 と読んで数値の条件が静かに壊れる。``is_actual`` で分ける。
    """
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["symbol"] = frame["Code"].map(_normalize_code)

    # 先読みを避ける基準はここ。決算期末（CurPerEn）ではない
    times = (
        frame["DiscTime"].astype(str)
        if "DiscTime" in frame.columns
        else pd.Series("00:00:00", index=frame.index)
    )
    frame["disclosed_at"] = pd.to_datetime(
        frame["DiscDate"].astype(str) + " " + times, errors="coerce"
    )
    frame["period_end"] = pd.to_datetime(frame.get("CurPerEn"), errors="coerce")
    frame["doc_type"] = frame.get("DocType", "").astype(str)
    frame["is_actual"] = frame["doc_type"].str.contains(ACTUAL_DOC_MARKER)

    # 空文字は 0 ではなく欠損。ここを間違えると「売上ゼロの会社」が現れる
    for source, target in (
        ("Sales", "sales"),
        ("OP", "operating_profit"),
        ("NP", "net_profit"),
        ("EPS", "eps"),
        ("Eq", "equity"),
        ("TA", "total_assets"),
        ("BPS", "bps"),
    ):
        if source in frame.columns:
            frame[target] = pd.to_numeric(
                frame[source].replace("", pd.NA), errors="coerce"
            )
    return frame


def _bars_frame(rows: list[dict]) -> pd.DataFrame:
    """API の返す文字列を数値に直し、列名を内部表記へ揃える。

    J-Quants は数値も JSON 文字列で返し、欠損は空文字になる。
    変換を忘れると文字列比較で静かに誤った結果になる。
    """
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["symbol"] = frame["Code"].map(_normalize_code)
    frame["date"] = pd.to_datetime(frame["Date"])

    mapping = {
        # 調整後（分割・併合を遡及反映）。リターンの計算はこちら
        "AdjO": "open",
        "AdjH": "high",
        "AdjL": "low",
        "AdjC": "close",
        "AdjVo": "volume",
        # 調整前（不変）。価格水準の判定はこちら
        "O": "raw_open",
        "H": "raw_high",
        "L": "raw_low",
        "C": "raw_close",
        "Vo": "raw_volume",
        "Va": "turnover",
        "AdjFactor": "adj_factor",
        "MktCap": "market_cap",
    }
    for source, target in mapping.items():
        if source in frame.columns:
            frame[target] = pd.to_numeric(
                frame[source].replace("", pd.NA), errors="coerce"
            )

    # ストップ高・安のフラグ。約定可否の判定に使う
    for source, target in (("UL", "limit_up"), ("LL", "limit_down")):
        if source in frame.columns:
            frame[target] = pd.to_numeric(frame[source], errors="coerce").fillna(0) != 0
        else:
            frame[target] = False

    keep = [
        "date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "raw_close",
        "turnover",
        "adj_factor",
        "market_cap",
        "limit_up",
        "limit_down",
    ]
    return frame[[c for c in keep if c in frame.columns]]


def free_plan_window(years: float = 2.0) -> tuple[dt.date, dt.date]:
    """Free プランで実際に取得できる期間（直近12週間を除く2年間）。

    終端を遅延ぶん手前に置く。today を終端にすると直近12週間が
    丸ごと空になり、その期間の検証結果が「取引なし」になる。
    """
    end = dt.date.today() - dt.timedelta(days=FREE_PLAN_DELAY_DAYS)
    return end - dt.timedelta(days=int(years * 365)), end


def estimate_fetch_minutes(trading_days: int, plan: str = DEFAULT_PLAN) -> float:
    """日付単位で取り込むときの最短所要時間（分）。

    Free の 5回/分 では2年分で100分近くかかる。
    「待てる時間なのか」を取り込み前に出すために使う。
    """
    per_minute = RATE_LIMITS.get(plan.lower())
    if not per_minute:
        raise JQuantsError(f"未知のプラン: {plan}")
    return trading_days / per_minute
