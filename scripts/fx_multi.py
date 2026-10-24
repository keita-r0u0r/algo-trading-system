"""FX の候補を、複数の通貨ペアでまとめて検証する（GMOコインの実際の1分足）。

  python scripts/fx_multi.py                     # USD_JPY / EUR_JPY / GBP_JPY / AUD_JPY
  python scripts/fx_multi.py --symbols USD_JPY,EUR_JPY

1つの通貨ペアだけでは取引の件数が足りず、偶然と区別がつかなかった。数値を変えずに他の通貨ペアでも試し、
件数を増やして見る。ドル円で決めたルールを他の通貨ペアで試すのは、ドル円に合わせ込んでいないかの確認にもなる。

  * 条件は fx_real.py と同じ（毎月1万円から、1回100通貨、実測スプレッド、API 手数料、ロスカット100%）
  * 各通貨ペアとも、最後の3か月は取り置く（見ない）
  * 試行回数は「候補 × 通貨ペア」で数える。これまでに同じデータで試した分も足す
  * スワップは全通貨ペアで同じ値を **仮定** している（実際は通貨ペアごとに違う）
  * 各月の前に、直前40日の足を「助走」として戦略に見せる（売買はしない）。日足20日の突破のように
    過去の足が要る候補が、月の初めに何も知らない状態で始まらないようにする
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import fx_real  # noqa: E402
from jqbt.core.selection import expected_max_sharpe  # noqa: E402
from jqbt.infrastructure import experiment_log  # noqa: E402
from jqbt.infrastructure.gmo_fx_store import load  # noqa: E402
from jqbt.strategies.fx import BuyAndHoldFx, DailyBreakout, HourlyBreakout, LondonBreakout, NoTradeFx  # noqa: E402

#: 候補（名前 → (作り方, 日をまたぐか)）。数値はドル円で決めたものを変えない
CANDIDATES = {
    "london_breakout": (LondonBreakout, False),
    "hourly_breakout_20": (HourlyBreakout, True),
    "daily_breakout_20": (DailyBreakout, True),
}
BASELINES = {"no_trade": (NoTradeFx, False), "buy_and_hold": (BuyAndHoldFx, True)}


def t_stat(values: list[float]) -> float | None:
    a = np.asarray(values, dtype=float)
    if len(a) < 2 or a.std(ddof=1) == 0:
        return None
    return float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default="USD_JPY,EUR_JPY,GBP_JPY,AUD_JPY")
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--units", type=float, default=100)
    parser.add_argument("--slippage", type=float, default=0.1)
    parser.add_argument("--stop-slippage", type=float, default=0.3)
    parser.add_argument("--max-entry-spread", type=float, default=1.0)
    parser.add_argument("--swap-long", type=float, default=0.012)
    parser.add_argument("--swap-short", type=float, default=-0.013)
    parser.add_argument("--lockbox-months", type=int, default=3)
    parser.add_argument("--random-seeds", type=int, default=10)
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    current = f"{dt.date.today():%Y-%m}"

    results: dict[str, dict[str, dict]] = {name: {} for name in [*CANDIDATES, *BASELINES, "random"]}
    for symbol in symbols:
        args.symbol = symbol
        months = fx_real.by_month(load(symbol))
        months.pop(current, None)
        names = list(months)
        dev = {m: months[m] for m in names[: -args.lockbox_months]}
        bars_all = pd.concat(months.values())
        warmup = {m: bars_all[(bars_all.index < b.index[0]) & (bars_all.index >= b.index[0] - pd.Timedelta(days=40))]
                  for m, b in dev.items()}
        data_end = str(names[-1])
        config = fx_real.gmo_config(args)
        print(f"{symbol}: 開発期間 {names[0]} 〜 {list(dev)[-1]}（{len(dev)} か月）", flush=True)
        for name, (make, overnight) in {**CANDIDATES, **BASELINES}.items():
            r = fx_real.run_months(dev, make, config, overnight, warmup)
            results[name][symbol] = r
            if name in CANDIDATES:
                experiment_log.append({
                    "kind": "fx_dev", "strategy": name, "label": name, "symbol": symbol, "max_entry_spread": 1.0,
                    "window": [names[0], list(dev)[-1]], "data_end": data_end, "cash": args.cash, "units": args.units,
                    "mean_monthly_return": r["mean_return"], "positive_months": r["positive_months"],
                    "months": r["months"], "trades": r["trades"], "expectancy": r["expectancy"], "t_stat": r["t"],
                })
        runs = [fx_real.run_months(dev, lambda s=s: fx_real.RANDOM["low_freq"](s), config, False, warmup)
                for s in range(args.random_seeds)]
        results["random"][symbol] = {"returns": [x for r in runs for x in r["returns"]], "pnls": [],
                                     "mean_return": float(np.mean([r["mean_return"] for r in runs]))}

    tried = {(r.get("label"), r.get("symbol")) for r in experiment_log.load() if r.get("kind") == "fx_dev"}
    trials = len(tried)
    print(f"\n【通貨ペアごとの月の平均（t 値）】毎月1万円から。これまでの FX の試行回数 {trials} 通り（候補 × 通貨ペア）")
    print("  " + f"{'戦略':<20}" + "".join(f"{s:>18}" for s in symbols) + f"{'まとめ':>22}")
    pooled_rows = {}
    for name in results:
        cells, pnls, returns = [], [], []
        for s in symbols:
            r = results[name][s]
            returns += r["returns"]
            pnls += r.get("pnls", [])
            t = r.get("t")
            cells.append(f"{r['mean_return']:+.2%}" + (f" ({t:+.1f})" if t is not None else ""))
        t_all = t_stat(pnls)
        pooled_rows[name] = (float(np.mean(returns)), sum(x > 0 for x in returns), len(returns), t_all, len(pnls))
        mean, up, n, t_all, k = pooled_rows[name]
        label = f"{mean:+.2%} {up}/{n}月" + (f" t={t_all:+.2f}" if t_all is not None else "")
        print("  " + f"{name:<20}" + "".join(f"{c:>18}" for c in cells) + f"{label:>22}")

    print("\n【判定】（まとめの t 値。取引は全通貨ペアを合わせた件数）")
    for name in CANDIDATES:
        mean, up, n, t_all, k = pooled_rows[name]
        expected = expected_max_sharpe(trials, max(k, 2))
        needed = max(2.0, (expected or 0) * math.sqrt(max(k, 2)))
        beat_random = mean > pooled_rows["random"][0]
        verdict = "否定されていない（取り置き期間で確かめる候補）" if (t_all or 0) >= needed and beat_random else "偶然と区別がつかない"
        print(f"  {name:<20} 取引 {k} 件 / t = {t_all or 0:+.2f}（{trials} 通り試せば偶然でも約 {needed:.2f}）→ {verdict}")
    print("  ※ スワップは全通貨ペアで同じ値を仮定。取り置いた最後の3か月は使っていません")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
