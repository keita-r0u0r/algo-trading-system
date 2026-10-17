"""J-Quants V2 の列名が、こちらの想定と一致していることの検証。

公式サンプル（``sample_data_v2``）をそのまま置いて、パーサに通す。

**なぜテストにするか。** 列名のズレは例外にならず、静かに欠損になる。
``AdjC`` を ``AdjClose`` と書き間違えても、``if source in frame.columns``
で素通りして close 列が消えるだけで、バックテストは「データがない日」
として何事もなく走る。API を叩かずに気づける唯一の場所がここ。

実データではなく仕様確認用のサンプルなので、**値の妥当性は検証しない。**
見ているのは列名と型だけ。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from jqbt.infrastructure.jquants import _bars_frame, _calendar_frame, _normalize_code

FIXTURES = Path(__file__).parent / "fixtures" / "jquants"

#: J-Quants の公式サンプル（sample_data_v2）は、再配布の可否を確認できていないので
#: リポジトリに含めない。手元に置いたときだけ検証する（tests/fixtures/jquants/ に配置）
pytestmark = pytest.mark.skipif(
    not FIXTURES.exists(), reason="J-Quants の公式サンプルが tests/fixtures/jquants/ に無い"
)


def rows(name: str) -> list[dict]:
    """API が返す形（文字列の辞書のリスト）に合わせて読む。"""
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ----------------------------------------------------------------------
# 株価四本値
# ----------------------------------------------------------------------
def test_daily_bars_columns_map_to_internal_names():
    frame = _bars_frame(rows("Stock_Prices_OHLC.csv"))

    assert not frame.empty
    #: 内部表記が全て揃っていること。1つでも欠けたら列名のズレ
    for column in (
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
    ):
        assert column in frame.columns, f"{column} がありません（API の列名が変わった可能性）"

    #: 文字列のまま残っていないこと（比較が静かに壊れる）
    for column in ("open", "high", "low", "close", "volume", "raw_close"):
        assert pd.api.types.is_numeric_dtype(frame[column])
        assert frame[column].notna().all(), f"{column} に欠損があります"

    assert pd.api.types.is_bool_dtype(frame["limit_up"])
    assert pd.api.types.is_bool_dtype(frame["limit_down"])


def test_adjusted_and_raw_are_kept_separately():
    """調整後と調整前が別の列に入っていること。

    同じ列に入れてしまうと、価格水準の判定で先読みが起きる。
    """
    frame = _bars_frame(rows("Stock_Prices_OHLC.csv"))
    assert "close" in frame.columns and "raw_close" in frame.columns

    source = rows("Stock_Prices_OHLC.csv")[0]
    assert frame["close"].iloc[0] == pytest.approx(float(source["AdjC"]))
    assert frame["raw_close"].iloc[0] == pytest.approx(float(source["C"]))


def test_code_is_normalised_to_four_digits():
    """J-Quants の5桁コードを4桁に直すこと。86970 → 8697。"""
    frame = _bars_frame(rows("Stock_Prices_OHLC.csv"))
    assert set(frame["symbol"]) == {"8697"}
    #: 末尾が0でない5桁（優先株など）はそのまま残す
    assert _normalize_code("25935") == "25935"


# ----------------------------------------------------------------------
# 取引カレンダー
# ----------------------------------------------------------------------
def test_trading_calendar_uses_the_v2_holiday_column():
    """V2 は ``HolDiv``。V1 の ``HolidayDivision`` とは別名。

    ここを間違えると全日が営業日扱いになり、休場日にも
    「約定した」ことになる。
    """
    frame = pd.DataFrame(rows("Trading_Calendar.csv"))
    assert "HolDiv" in frame.columns

    parsed = _calendar_frame(frame)
    #: 2022-01-01 は休場
    assert not bool(parsed.loc[parsed["date"] == pd.Timestamp("2022-01-01"), "is_open"].iloc[0])
    #: 2022-01-04 は大発会
    assert bool(parsed.loc[parsed["date"] == pd.Timestamp("2022-01-04"), "is_open"].iloc[0])


# ----------------------------------------------------------------------
# 財務
# ----------------------------------------------------------------------
def test_statements_have_a_disclosure_timestamp_not_a_period_end():
    """開示日時（DiscDate/DiscTime）が存在すること。

    決算期末（CurPerEn）で判断すると、まだ公表されていない数字で
    売買したことになる。この2列が消えたら、その時点で先読みが入る。
    """
    frame = pd.DataFrame(rows("Financial_Data.csv"))
    for column in ("DiscDate", "DiscTime", "CurPerEn", "Code"):
        assert column in frame.columns

    disclosed = pd.to_datetime(
        frame["DiscDate"].astype(str) + " " + frame["DiscTime"].astype(str),
        errors="coerce",
    )
    assert disclosed.notna().all()

    #: **実績** の開示は必ず決算期末以降
    period_end = pd.to_datetime(frame["CurPerEn"], errors="coerce")
    actuals = frame["DocType"].str.contains("FinancialStatements")
    assert (disclosed[actuals].dt.date >= period_end[actuals].dt.date).all()


def test_forecast_revisions_are_disclosed_before_the_period_ends():
    """``fins/summary`` は決算短信だけではない。

    ``EarnForecastRevision``（業績予想の修正）は **期中に** 出るので、
    CurPerEn より前の開示日を持つ。しかも Sales などの実績値は空。
    これを実績と混ぜると、
      - 期末より前の日付で「決算が出た」ことになる
      - 空の売上を 0 と読んで、数値の条件が壊れる
    どちらも静かに起きるので、DocType で必ず選り分けること。
    """
    frame = pd.DataFrame(rows("Financial_Data.csv"))
    revisions = frame[frame["DocType"] == "EarnForecastRevision"]
    assert not revisions.empty, "サンプルに予想修正が含まれていません"

    disclosed = pd.to_datetime(revisions["DiscDate"])
    period_end = pd.to_datetime(revisions["CurPerEn"])
    assert (disclosed < period_end).all()
    #: 実績値は入っていない
    assert (revisions["Sales"] == "").all()


def test_statements_marks_actuals_separately(tmp_path):
    """パーサが実績と予想修正を区別できる形で返すこと。"""
    from jqbt.infrastructure.jquants import _statements_frame

    parsed = _statements_frame(rows("Financial_Data.csv"))
    assert "doc_type" in parsed.columns
    assert "is_actual" in parsed.columns
    assert parsed["is_actual"].sum() == 4
    assert (~parsed["is_actual"]).sum() == 1
    #: 実績だけを見れば、開示は必ず期末以降
    actual = parsed[parsed["is_actual"]]
    assert (actual["disclosed_at"].dt.date >= actual["period_end"].dt.date).all()


# ----------------------------------------------------------------------
# 銘柄一覧
# ----------------------------------------------------------------------
def test_listed_master_has_a_date_column():
    """``date`` 付きで返ること。

    日付がないなら「今日の一覧」で、生存者バイアスが入る。
    """
    frame = pd.DataFrame(rows("Listed_Issue_Master.csv"))
    assert "Date" in frame.columns
    assert "Code" in frame.columns
    assert frame["Date"].iloc[0] == "2022-01-04"
