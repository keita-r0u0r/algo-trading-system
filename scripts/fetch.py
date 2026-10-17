"""J-Quants から日足と銘柄一覧を取り込んで Parquet に落とす。

    export JQUANTS_API_KEY=...
    python scripts/fetch.py --years 2

取得は **日付単位** で行う（銘柄単位より圧倒的に呼び出し回数が少ない）。
1営業日ぶんが1ファイルになるので、途中で止めても続きから再開できる。

銘柄一覧は **月次で date を指定して** 取る。これが生存者バイアスを
避けるための唯一の入口で、省略すると「今日上場している銘柄」しか残らない。
月次にしているのは、上場・廃止の頻度から見て日次まで刻む必要がないため。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.infrastructure.env import load_env
from jqbt.infrastructure.secrets import SecretError, build_secrets, require

load_env(Path(__file__).resolve().parent)

from jqbt.infrastructure import parquet_store as dataset
from jqbt.infrastructure.jquants import (
    JQuantsClient,
    JQuantsError,
    estimate_fetch_minutes,
    free_plan_window,
)


def month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    days = []
    cursor = dt.date(start.year, start.month, 1)
    while cursor <= end:
        days.append(max(cursor, start))
        cursor = (
            dt.date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else dt.date(cursor.year, cursor.month + 1, 1)
        )
    return days


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--start", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--skip-master", action="store_true")
    parser.add_argument(
        "--rebuild-only",
        action="store_true",
        help="取得済みの raw からパネルを作り直すだけ",
    )
    args = parser.parse_args()

    default_start, default_end = free_plan_window(args.years)
    start = args.start or default_start
    end = args.end or default_end

    if not args.rebuild_only:
        # キーは秘密情報の取り出し口から読む（GCP Secret Manager / .env）。
        # GCP を使う設定で読めなければ、.env に戻らずに止める
        secrets = build_secrets()
        try:
            api_key = require(secrets, "jquants_api_key")
        except SecretError as exc:
            print(
                f"{exc}\n"
                "  キーは https://jpx-jquants.com/ のダッシュボード [API Keys] で発行します。\n"
                "  置き場所は docs/secrets.md を参照してください。\n"
                "  それまでは scripts/run.py --synthetic で仕組みだけ確認できます。"
            )
            return 1
        print(f"APIキーの読み込み元: {secrets.describe()}")
        client = JQuantsClient(api_key=api_key)

        # 残りの営業日数から所要時間を出す。Free は5回/分なので、
        # 2年分だと100分近くかかる。走らせる前に分かるようにしておく
        have = set(dataset.raw_bar_dates())
        weekdays = [
            start + dt.timedelta(days=i)
            for i in range((end - start).days + 1)
            if (start + dt.timedelta(days=i)).weekday() < 5
        ]
        todo = [d for d in weekdays if d not in have]
        minutes = estimate_fetch_minutes(len(todo), client.plan)

        print(f"対象期間: {start} 〜 {end}")
        print("（Free プランは12週間遅延しますが、過去の再現には影響しません）")
        print(
            f"プラン: {client.plan} / 未取得 {len(todo)}日 → "
            f"最短 {minutes:.0f}分（レート制限のため）"
        )
        if len(todo) != len(weekdays):
            print(f"  取得済み {len(weekdays) - len(todo)}日はスキップします")
        print()

        # --- 銘柄一覧（月次・date 指定） ---
        if not args.skip_master:
            for day in month_starts(start, end):
                path = dataset.RAW_DIR / "master" / f"date={day.isoformat()}.parquet"
                if path.exists():
                    continue
                try:
                    frame = client.listed_master(day)
                except JQuantsError as exc:
                    print(f"  master {day}: {exc}")
                    continue
                if not frame.empty:
                    dataset.save_raw_master(frame, day)
                    print(f"  master {day}: {len(frame)}銘柄")

        # --- 日足（日付単位） ---
        cursor = start
        fetched = skipped = 0
        processed = 0
        began = time.monotonic()
        while cursor <= end:
            if cursor.weekday() >= 5 or cursor in have:
                cursor += dt.timedelta(days=1)
                continue
            processed += 1
            try:
                frame = client.daily_bars(date=cursor)
            except JQuantsError as exc:
                print(f"  bars {cursor}: {exc}")
                cursor += dt.timedelta(days=1)
                continue

            if frame.empty:
                skipped += 1  # 休場日
                note = "休場"
            else:
                dataset.save_raw_bars(frame, cursor)
                fetched += 1
                note = f"{len(frame)}銘柄"
            # 1日ごとに出す。レート制限で数時間かかるので、止まっていないことが見えるように
            elapsed = time.monotonic() - began
            remaining = elapsed / processed * (len(todo) - processed)
            print(
                f"  [{processed}/{len(todo)}] {cursor} {note}"
                f"  経過 {elapsed / 60:.0f}分 / 残り約{remaining / 60:.0f}分",
                flush=True,
            )
            cursor += dt.timedelta(days=1)

        print(f"\n取得: {fetched}営業日 / 休場等でスキップ: {skipped}日")

    # --- パネルに集約 ---
    panel = dataset.build_panel(start, end)
    path = dataset.save_panel(panel)
    days = len(set(panel.index.get_level_values("date")))
    symbols = len(set(panel.index.get_level_values("symbol")))
    print(f"パネル作成: {path}")
    print(f"  {days}営業日 × 延べ{symbols}銘柄 = {len(panel):,}行")

    report = dataset.capital_feasibility(panel, 300_000, 3)
    if report:
        print()
        print(dataset.format_feasibility(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
