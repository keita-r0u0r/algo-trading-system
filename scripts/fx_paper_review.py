"""FX のペーパー運用の判断を振り返る。AI の根拠と、その後の実際の値動きを並べる。

  python scripts/fx_paper_review.py                       # 手元の state/fx_paper.jsonl
  python scripts/fx_paper_review.py --log fx_paper.jsonl  # クラウドから持ってきた記録
  gcloud storage cp gs://<プロジェクト>-algo-trading-state/state/fx_paper.jsonl .   # クラウドの記録を持ってくる

判断ごとに、AI の根拠（確信度・想定する期間・理由）と、判断のあと1時間・4時間・24時間で
その通貨ペアがどれだけ動いたか（pips）を出す。見送った回に、コスト（往復約1 pip）を大きく
超える値動きがどれだけあったかが、改善の種になる。

**値動きは結果論。** 1回ごとに「入っていれば勝てた」と読まないこと。見るのは、根拠と値動きの傾向。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from jqbt.application.fx_paper import moves_after  # noqa: E402
from jqbt.infrastructure import fx_paper_log  # noqa: E402
from jqbt.infrastructure.gmo_fx import GmoFxClient  # noqa: E402
from jqbt.infrastructure.gmo_fx_store import fetch_day, to_engine  # noqa: E402

BASE = "USD_JPY"
#: 見送った回に「大きく動いた」とみなす幅（pips）。コストの約20倍
BIG_MOVE = 20.0


def bars_around(api: GmoFxClient, symbol: str, days: set[dt.date], cache: dict) -> pd.DataFrame:
    frames = []
    for day in sorted(days):
        key = (symbol, day)
        if key not in cache:
            raw = fetch_day(api, symbol, day) if day.weekday() < 5 else pd.DataFrame()
            if not raw.empty:
                raw["session"] = day
            cache[key] = to_engine(raw) if not raw.empty else raw
        if not cache[key].empty:
            frames.append(cache[key])
    return pd.concat(frames).sort_index() if frames else pd.DataFrame()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, default=fx_paper_log.DEFAULT_PATH)
    parser.add_argument("--account", default="ai")
    args = parser.parse_args()

    decisions = [e for e in fx_paper_log.load(args.log)
                 if e.get("kind") == "decision" and e.get("account") == args.account and "decision" in e]
    if not decisions:
        print(f"{args.log} に {args.account} の判断がありません")
        return 1

    api, cache, rows = GmoFxClient(), {}, []
    today = dt.date.today()
    for e in decisions:
        d = e["decision"]
        stamp = pd.Timestamp(e["at"])
        at = stamp.tz_localize(None) if stamp.tzinfo else stamp  # 日本時間のまま、時差の情報だけ外す
        symbol = d.get("symbol") or BASE
        # 判断の日から2日後までの営業日（24時間後まで見るため）。未来の日は取れないので除く
        days = {(at + pd.Timedelta(days=k)).date() for k in range(3)}
        moves = moves_after(bars_around(api, symbol, {x for x in days if x <= today}, cache), at)
        rows.append({
            "時刻": at.strftime("%m-%d %H:%M"),
            "判断": d.get("action"),
            "通貨": symbol,
            "確信度": d.get("confidence", ""),
            "期間": d.get("horizon", ""),
            "結果": "通過" if e.get("approved") else (e.get("rejected") or "見送り"),
            # 前提文の版。記録が無いものは最初の版（検証向けの慎重な前提文を共用していた）
            "版": e.get("prompt_version") or "fx-v1",
            **moves,
            "根拠": (d.get("reasoning") or "")[:120],
        })

    frame = pd.DataFrame(rows)
    with pd.option_context("display.max_colwidth", 60, "display.width", 200):
        print(frame.drop(columns=["根拠"]).to_string(index=False))
    print("\n【根拠】")
    for r in rows:
        print(f"  {r['時刻']} {r['判断']}（{r['確信度']}）: {r['根拠']}")

    holds = frame[frame["判断"] == "hold"]
    print("\n【まとめ】")
    for version, group in frame.groupby("版"):
        print(f"  {version}: 判断 {len(group)} 回: " + " / ".join(f"{k} {v}" for k, v in Counter(group['判断']).items()))
    if not holds.empty and "up_4h" in holds:
        big = holds[(holds["up_4h"] >= BIG_MOVE) | (holds["down_4h"] >= BIG_MOVE)]
        print(f"  見送った {len(holds)} 回のうち、4時間以内に ±{BIG_MOVE:g} pips 以上動いたのは {len(big)} 回"
              f"（{BASE} 以外を選べた可能性は含まない）")
    print("  ※ 値動きは結果論です。1回ごとの当たり外れではなく、根拠の偏りと傾向を見てください")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
