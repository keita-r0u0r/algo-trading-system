"""データの保管と読み出し。

## なぜ DB ではなく Parquet か

研究段階に RDB は要らない。日足は全上場銘柄10年でも1億行に届かず、
Parquet + pandas で十分に速い。DB を入れるとスキーマ移行とORMの都合が
研究の速度を殺す。SQL を投げたくなったら DuckDB から同じ Parquet を読める。

  data/
    raw/bars/date=YYYY-MM-DD.parquet   取得したまま（再取得しないための保管）
    raw/master/date=YYYY-MM-DD.parquet その日時点の上場銘柄一覧
    curated/panel.parquet              バックテストが読む1枚

## パネルの形

(date, symbol) の MultiIndex。列は open/high/low/close/volume ほか。
**その日に上場していなかった銘柄の行は存在しない。** 欠損値で埋めない。
埋めると「上場前から売買できた」ことになってしまう。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
RAW_DIR = DATA_DIR / "raw"
CURATED_DIR = DATA_DIR / "curated"
PANEL_PATH = CURATED_DIR / "panel.parquet"


def _ensure_dirs() -> None:
    for path in (RAW_DIR / "bars", RAW_DIR / "master", CURATED_DIR):
        path.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------
def save_raw_bars(frame: pd.DataFrame, day: dt.date) -> Path:
    _ensure_dirs()
    path = RAW_DIR / "bars" / f"date={day.isoformat()}.parquet"
    frame.to_parquet(path, index=False)
    return path


def save_raw_master(frame: pd.DataFrame, day: dt.date) -> Path:
    _ensure_dirs()
    path = RAW_DIR / "master" / f"date={day.isoformat()}.parquet"
    frame.to_parquet(path, index=False)
    return path


def raw_bar_dates() -> list[dt.date]:
    """取得済みの日付。再取得を避けるために使う。"""
    directory = RAW_DIR / "bars"
    if not directory.exists():
        return []
    return sorted(
        dt.date.fromisoformat(p.stem.split("=", 1)[1]) for p in directory.glob("*.parquet")
    )


# ----------------------------------------------------------------------
def build_panel(
    start: dt.date | None = None, end: dt.date | None = None
) -> pd.DataFrame:
    """raw の日次ファイルを1枚のパネルにまとめる。"""
    files = sorted((RAW_DIR / "bars").glob("*.parquet"))
    if not files:
        raise FileNotFoundError(
            "raw データがありません。先に scripts/fetch.py を実行してください"
        )

    frames = []
    for path in files:
        day = dt.date.fromisoformat(path.stem.split("=", 1)[1])
        if start and day < start:
            continue
        if end and day > end:
            continue
        frames.append(pd.read_parquet(path))

    if not frames:
        raise ValueError("指定期間に該当するデータがありません")

    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    panel = panel.dropna(subset=["close"])
    panel = panel.set_index(["date", "symbol"]).sort_index()
    return panel


def save_panel(panel: pd.DataFrame) -> Path:
    _ensure_dirs()
    panel.reset_index().to_parquet(PANEL_PATH, index=False)
    return PANEL_PATH


def load_panel(
    start: dt.date | None = None,
    end: dt.date | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    """バックテストが読む形で返す。"""
    if not PANEL_PATH.exists():
        raise FileNotFoundError(
            f"{PANEL_PATH} がありません。scripts/fetch.py で作成してください"
        )
    frame = pd.read_parquet(PANEL_PATH)
    frame["date"] = pd.to_datetime(frame["date"])
    if start is not None:
        frame = frame[frame["date"] >= pd.Timestamp(start)]
    if end is not None:
        frame = frame[frame["date"] <= pd.Timestamp(end)]
    if symbols:
        frame = frame[frame["symbol"].isin(symbols)]
    return frame.set_index(["date", "symbol"]).sort_index()


# ----------------------------------------------------------------------
def point_in_time_universe(day: dt.date) -> list[str]:
    """その日時点で上場していた銘柄。

    生存者バイアスを避けるための入口。``/equities/master`` を
    date 付きで取って保存してあるものを読む。

    保存がなければ **空リストを返す**。「分からないので全銘柄で代用する」
    のは静かにバイアスを入れる行為なので、ここではやらない。
    """
    path = RAW_DIR / "master" / f"date={day.isoformat()}.parquet"
    if not path.exists():
        return []
    frame = pd.read_parquet(path)
    return sorted(frame["symbol"].astype(str).unique())


def liquid_universe(
    panel: pd.DataFrame,
    as_of: dt.date,
    lookback: int = 60,
    min_turnover: float = 100_000_000.0,
    max_price: float | None = None,
) -> list[str]:
    """流動性で絞った銘柄群。

    ``max_price`` は **調整前終値** で判定する。調整後で判定すると
    将来の分割を知っていることになるため。

    少額運用では ``max_price`` が効く。資金30万円なら、単元100株で
    3,000円を超える銘柄は1単元すら買えない。
    """
    window = panel.loc[
        (panel.index.get_level_values("date") <= pd.Timestamp(as_of))
    ]
    if window.empty:
        return []

    recent_dates = sorted(set(window.index.get_level_values("date")))[-lookback:]
    if not recent_dates:
        return []
    recent = window.loc[window.index.get_level_values("date").isin(recent_dates)]

    turnover_col = "turnover" if "turnover" in recent.columns else None
    grouped = recent.groupby(level="symbol")

    result = []
    for symbol, rows in grouped:
        if len(rows) < lookback * 0.8:
            continue  # 上場直後・売買停止が多い銘柄は外す
        if turnover_col:
            if float(rows[turnover_col].mean()) < min_turnover:
                continue
        if max_price is not None:
            price_col = "raw_close" if "raw_close" in rows.columns else "close"
            if float(rows[price_col].iloc[-1]) > max_price:
                continue
        result.append(str(symbol))
    return sorted(result)


# ----------------------------------------------------------------------
def capital_feasibility(
    panel: pd.DataFrame, cash: float, max_positions: int, lot_size: int = 100
) -> dict:
    """その資金で、母集団の何割が実際に買えるか。

    **少額運用ではこれが戦略選択そのものを決める。**
    単元100株なので、資金30万円を3分割した10万円では
    株価1,000円を超える銘柄は1単元も買えない。
    「上位N銘柄を買う」系の戦略は、母集団が低位株に限定される。

    これは制約であると同時にバイアスでもある。低位株は
    ボラティリティ・流動性・業種構成が市場平均と違うため、
    全銘柄で検証した結果をそのまま適用できない。
    """
    if panel.empty:
        return {}

    last_date = max(panel.index.get_level_values("date"))
    today = panel.xs(last_date, level="date")
    price_col = "raw_close" if "raw_close" in today.columns else "close"
    prices = today[price_col].dropna()
    if prices.empty:
        return {}

    budget = cash / max(max_positions, 1)
    unit_cost = prices * lot_size
    affordable = unit_cost <= budget

    return {
        "as_of": last_date.date(),
        "cash": cash,
        "max_positions": max_positions,
        "budget_per_position": budget,
        "universe_size": int(len(prices)),
        "affordable_count": int(affordable.sum()),
        "affordable_ratio": float(affordable.mean()),
        "lot_size": lot_size,
        "max_affordable_price": float(budget / lot_size),
        "median_price": float(prices.median()),
        "cash_for_full_universe": float(prices.max() * lot_size * max_positions),
    }


def format_feasibility(report: dict) -> str:
    if not report:
        return ""
    lines = [
        "【資金と単元株の制約】",
        f"  1銘柄あたり予算      {report['budget_per_position']:,.0f}円"
        f"（{report['cash']:,.0f}円 ÷ {report['max_positions']}銘柄）",
        f"  買える株価の上限      {report['max_affordable_price']:,.0f}円"
        f"（売買単位{report.get('lot_size', 100)}株のため）",
        f"  買える銘柄            {report['affordable_count']} / {report['universe_size']}"
        f"（{report['affordable_ratio'] * 100:.0f}%）",
        f"  母集団の中央値株価    {report['median_price']:,.0f}円",
    ]
    if report["affordable_ratio"] < 0.5:
        lines.append(
            f"  ! 母集団の{(1 - report['affordable_ratio']) * 100:.0f}%は資金不足で買えません。"
        )
        lines.append(
            "    検証結果は実質的に低位株のみのものになり、市場全体の性質とは異なります。"
        )
        lines.append(
            f"    全銘柄を対象にするには約{report['cash_for_full_universe']:,.0f}円必要です。"
        )
    return "\n".join(lines)
