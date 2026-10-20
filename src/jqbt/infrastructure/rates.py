"""日米の国債利回り（日次）。金利差の売買ルールに使う。

| | 出どころ | 形 |
|---|---|---|
| 日本 | 財務省「国債金利情報」 https://www.mof.go.jp/jgbs/reference/interest_rate/ | Shift_JIS の CSV。日付は和暦（R8.9.30） |
| 米国 | セントルイス連銀 FRED（DGS2 など） https://fred.stlouisfed.org/ | CSV。キー不要 |

``data/rates/<名前>.csv`` に「日付,利回り(%)」で保存する。

## いつ分かる値か（先読みを防ぐ）

日付はその国の取引日。米国の D 日の値は、日本時間 D+1 日の朝（米国の引け後）に分かる。
日本の D 日の値は D 日の夕方に分かる。だから **日本時間 D 日の判断には、D−1 日までの値だけ** を使う
（``known_before``）。
"""

from __future__ import annotations

import datetime as dt
import io
import re
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[3] / "data" / "rates"
MOF_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
MOF_CURRENT = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
ERAS = {"M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018}


def parse_wareki(text: str) -> dt.date | None:
    """R8.9.30 → 2026-09-30。読めなければ None。"""
    m = re.fullmatch(r"([MTSHR])(\d+)\.(\d+)\.(\d+)", text.strip())
    if not m:
        return None
    era, year, month, day = m.groups()
    return dt.date(ERAS[era] + int(year), int(month), int(day))


def parse_mof(raw: bytes, tenor: str = "2年") -> pd.Series:
    text = raw.decode("shift_jis", errors="replace")
    lines = text.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("基準日"))
    frame = pd.read_csv(io.StringIO("\n".join(lines[header:])), dtype=str)
    dates = frame["基準日"].map(lambda v: parse_wareki(v) if isinstance(v, str) else None)
    values = pd.to_numeric(frame[tenor], errors="coerce")
    series = pd.Series(values.to_numpy(), index=dates).dropna()
    series = series[series.index.notna()]
    series.index = pd.DatetimeIndex(series.index, name="date")
    return series.sort_index()


def parse_fred(raw: bytes) -> pd.Series:
    frame = pd.read_csv(io.BytesIO(raw))
    series = pd.Series(pd.to_numeric(frame.iloc[:, 1], errors="coerce").to_numpy(),
                       index=pd.DatetimeIndex(pd.to_datetime(frame.iloc[:, 0]), name="date"))
    return series.dropna().sort_index()


def fetch(client: httpx.Client | None = None, root: Path = ROOT) -> dict[str, pd.Series]:
    """日米の2年・10年を取り込み、保存する。"""
    http = client or httpx.Client(timeout=60, follow_redirects=True)
    jp_raw = http.get(MOF_ALL)
    jp_raw.raise_for_status()
    current = http.get(MOF_CURRENT)
    current.raise_for_status()
    out = {}
    for tenor, name in (("2年", "jp2y"), ("10年", "jp10y")):
        series = pd.concat([parse_mof(jp_raw.content, tenor), parse_mof(current.content, tenor)])
        out[name] = series[~series.index.duplicated(keep="last")].sort_index()
    for series_id, name in (("DGS2", "us2y"), ("DGS10", "us10y")):
        response = http.get(FRED.format(series=series_id))
        response.raise_for_status()
        out[name] = parse_fred(response.content)
    root.mkdir(parents=True, exist_ok=True)
    for name, series in out.items():
        series.rename("yield").to_csv(root / f"{name}.csv")
    return out


def load(name: str, root: Path = ROOT) -> pd.Series:
    path = root / f"{name}.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} がありません。先に取り込んでください（scripts/fx_real.py --fetch-rates）")
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    return frame.iloc[:, 0].sort_index()


def spread(us: str = "us2y", jp: str = "jp2y", root: Path = ROOT) -> pd.Series:
    """米国 − 日本 の利回りの差（%）。両方の値がある日だけ。"""
    a, b = load(us, root), load(jp, root)
    joined = pd.concat([a.rename("us"), b.rename("jp")], axis=1).dropna()
    return (joined["us"] - joined["jp"]).rename("spread")


def known_before(series: pd.Series, day: dt.date) -> pd.Series:
    """日本時間 ``day`` の判断に使ってよい値（day より前の日付）。"""
    return series[series.index < pd.Timestamp(day)]
