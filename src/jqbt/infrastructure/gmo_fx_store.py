"""GMOコイン 外国為替FX の1分足の保管（Parquet、1営業日1ファイル）。

``data/fx/gmo/<通貨ペア>/1min/<YYYYMMDD>.parquet`` に、売値（BID）と買値（ASK）の
始値・高値・安値・終値を並べて置く。

## 営業日

API の ``date`` は GMO の営業日（朝6時または7時に始まり、翌朝に終わる）。
日をまたがないデイトレの「1日」も、この営業日で区切る（``session`` の列）。

## 取り込み

  * 取得済みの日は飛ばす（途中で止めても再開できる）
  * 今日（まだ終わっていない営業日）は保存しない
  * 足の無い日（祝日など）も空のファイルを置き、次から問い合わせない
  * 1秒に4回までに抑える。公開 API の上限は書かれていないので、控えめにする
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Callable

import httpx
import numpy as np
import pandas as pd

from .gmo_fx import GmoError, GmoFxClient

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3] / "data" / "fx" / "gmo"
#: 1分足が取れる最初の日（API の説明より）
FIRST_DAY = dt.date(2023, 10, 28)
JST = dt.timezone(dt.timedelta(hours=9))
COLUMNS = [f"{side}_{f}" for side in ("bid", "ask") for f in ("open", "high", "low", "close")]


def day_path(symbol: str, day: dt.date, root: Path = ROOT) -> Path:
    return root / symbol / "1min" / f"{day:%Y%m%d}.parquet"


def _frame(rows: list[dict], side: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=[f"{side}_{f}" for f in ("open", "high", "low", "close")])
    df = pd.DataFrame(rows)
    index = pd.to_datetime(df["openTime"].astype("int64"), unit="ms", utc=True).dt.tz_convert(JST).dt.tz_localize(None)
    out = df[["open", "high", "low", "close"]].astype(float)
    out.index = pd.DatetimeIndex(index, name="time")
    return out.add_prefix(f"{side}_")


def fetch_day(api: GmoFxClient, symbol: str, day: dt.date) -> pd.DataFrame:
    date = f"{day:%Y%m%d}"
    bid = _frame(api.klines(symbol, "1min", date, "BID"), "bid")
    ask = _frame(api.klines(symbol, "1min", date, "ASK"), "ask")
    return bid.join(ask, how="inner")[COLUMNS]


def fetch_range(
    api: GmoFxClient,
    symbol: str,
    start: dt.date,
    end: dt.date,
    root: Path = ROOT,
    interval: float = 0.25,
    progress: Callable[[str], None] = print,
) -> int:
    """``start``〜``end`` の営業日を取り込む。新しく保存した日数を返す。"""
    today = dt.datetime.now(JST).date()
    days = [d.date() for d in pd.date_range(max(start, FIRST_DAY), min(end, today - dt.timedelta(days=1)))]
    # 営業日は月〜金（土曜の朝までは金曜の営業日に入る）
    todo = [d for d in days if d.weekday() < 5 and not day_path(symbol, d, root).exists()]
    started, saved = time.monotonic(), 0
    for k, day in enumerate(todo, 1):
        for attempt in range(5):
            try:
                frame = fetch_day(api, symbol, day)
                break
            except (httpx.HTTPError, GmoError) as exc:
                wait = 2 ** attempt * 5
                logger.warning("%s の取得に失敗（%s）。%d秒後にやり直します", day, type(exc).__name__, wait)
                time.sleep(wait)
        else:
            raise RuntimeError(f"{day} を取得できませんでした")
        path = day_path(symbol, day, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path)
        saved += 1
        if k % 20 == 0 or k == len(todo):
            elapsed = time.monotonic() - started
            eta = elapsed / k * (len(todo) - k)
            progress(f"  {day}: {k}/{len(todo)} 日（{len(frame)} 本） 残り約 {eta / 60:.0f} 分")
        time.sleep(interval)
    return saved


def load(symbol: str, start: dt.date | None = None, end: dt.date | None = None, root: Path = ROOT, pip: float = 0.01) -> pd.DataFrame:
    """保存した足を、検証エンジンの形（仲値の OHLC・スプレッド・営業日）にして返す。

    ``spread`` は始値の時点の買値 − 売値（pips）。エンジンは足の始値で約定させるため。
    """
    files = sorted((root / symbol / "1min").glob("*.parquet"))
    frames = []
    for path in files:
        day = dt.datetime.strptime(path.stem, "%Y%m%d").date()
        if (start and day < start) or (end and day > end):
            continue
        frame = pd.read_parquet(path)
        if frame.empty:
            continue
        frame["session"] = day
        frames.append(frame)
    if not frames:
        raise FileNotFoundError(f"{root / symbol} に足がありません。先に取り込んでください")
    raw = pd.concat(frames)
    out = pd.DataFrame(index=raw.index)
    for f in ("open", "high", "low", "close"):
        out[f] = (raw[f"bid_{f}"] + raw[f"ask_{f}"]) / 2
    out["spread"] = np.maximum((raw["ask_open"] - raw["bid_open"]) / pip, 0.0)
    out["session"] = raw["session"]
    return out
