"""FX の候補を、複数の通貨ペアでまとめて検証する（GMOコインの実際の1分足）。

  python scripts/fx_multi.py                     # 値動きのルール（price）を USD_JPY / EUR_JPY / GBP_JPY / AUD_JPY で
  python scripts/fx_multi.py --set carry         # 金利差を持つ候補（carry）。月で区切らず続けて評価する
  python scripts/fx_multi.py --symbols USD_JPY,EUR_JPY

1つの通貨ペアだけでは取引の件数が足りず、偶然と区別がつかなかった。数値を変えずに他の通貨ペアでも試し、
件数を増やして見る。ドル円で決めたルールを他の通貨ペアで試すのは、ドル円に合わせ込んでいないかの確認にもなる。

  * 条件は fx_real.py と同じ（毎月1万円から、1回100通貨、実測スプレッド、API 手数料、ロスカット100%）
  * 各通貨ペアとも、最後の3か月は取り置く（見ない）
  * 試行回数は「候補 × 通貨ペア」で数える。これまでに同じデータで試した分も足す
  * スワップは、各通貨の短期金利の差から日ごとに見積もる（業者の取り分 0.5% を両側に引く）。
    使うのは前の月までに分かっていた金利だけ。``--swap fixed`` で以前の一定の仮定に戻せる
  * carry の組は、数週間〜数か月持つので、月で区切らず開発期間を通して1回で評価する（月ごとの損益は資産曲線から出す）
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
from jqbt.application.fx_backtest import run_fx  # noqa: E402
from jqbt.core.fx.model import SWAP_MARKUP, swap_per_unit  # noqa: E402
from jqbt.infrastructure import experiment_log, rates  # noqa: E402
from jqbt.infrastructure.gmo_fx_store import load  # noqa: E402
from jqbt.strategies.fx import (  # noqa: E402
    BuyAndHoldFx,
    CarryFilter,
    DailyBreakout,
    HourlyBreakout,
    LondonBreakout,
    NoTradeFx,
)

#: 業者のスワップの取り分（%）。結果を見る前に決めた
MARKUP = SWAP_MARKUP
#: 候補の組（名前 → (作り方, 日をまたぐか)）。作り方は、その通貨ペアの金利差（%）を受け取る
SETS = {
    # 値動きだけのルール。数値はドル円で決めたものを変えない
    "price": {
        "london_breakout": (lambda carry: LondonBreakout(), False),
        "hourly_breakout_20": (lambda carry: HourlyBreakout(), True),
        "daily_breakout_20": (lambda carry: DailyBreakout(), True),
    },
    # 金利差を持つ。条件が崩れたら降りる（2周目の Act）
    "carry": {
        "carry_trend": (lambda carry: CarryFilter(carry, "trend"), True),
        "carry_calm": (lambda carry: CarryFilter(carry, "calm"), True),
    },
}
BASELINES = {"no_trade": (lambda carry: NoTradeFx(), False), "buy_and_hold": (lambda carry: BuyAndHoldFx(), True)}


def carry_series(symbol: str, days: pd.DatetimeIndex) -> pd.Series:
    """その日に分かっていた「受け取れる金利差」（%、買いの側。取り分を引いた後）。"""
    base, quote = symbol.split("_")
    return (rates.short_rate_known(base, days) - rates.short_rate_known(quote, days) - MARKUP).dropna()


def with_swaps(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """営業日ごとのスワップ（円/通貨/日）の列を足す。"""
    base, quote = symbol.split("_")
    sessions = pd.DatetimeIndex(sorted(set(bars["session"])))
    b, q = rates.short_rate_known(base, sessions), rates.short_rate_known(quote, sessions)
    first_close = bars.groupby("session")["close"].first()
    table = {}
    for day in sessions:
        price = float(first_close[day.date()])
        table[day.date()] = swap_per_unit(price, float(b[day]), float(q[day]), MARKUP)
    out = bars.copy()
    out["swap_long"] = [table[d][0] for d in out["session"]]
    out["swap_short"] = [table[d][1] for d in out["session"]]
    return out


def run_continuous(bars: pd.DataFrame, make, config, overnight: bool) -> dict:
    """開発期間を通して1回で走らせる。月ごとの損益は、月末の資産の変化から出す。"""
    from dataclasses import replace

    result = run_fx(bars, make(), replace(config, flat_at_day_end=not overnight))
    curve = pd.Series(result.equity, index=pd.to_datetime([pd.Timestamp(d) for d in result.days]))
    month_end = curve.groupby(curve.index.to_period("M")).last()
    previous = month_end.shift(1).fillna(config.initial_cash)
    returns = list((month_end / previous - 1).to_numpy())
    pnls = [t.pnl for t in result.trades]
    a = np.asarray(pnls)
    return {
        "returns": returns, "mean_return": float(np.mean(returns)), "worst_month": float(np.min(returns)),
        "positive_months": sum(r > 0 for r in returns), "months": len(returns), "trades": len(pnls),
        "expectancy": float(a.mean()) if len(a) else None, "t": t_stat(pnls), "pnls": pnls,
        "final": float(result.equity[-1]), "max_drawdown": result.summary["max_drawdown"],
    }


def t_stat(values: list[float]) -> float | None:
    a = np.asarray(values, dtype=float)
    if len(a) < 2 or a.std(ddof=1) == 0:
        return None
    return float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default="USD_JPY,EUR_JPY,GBP_JPY,AUD_JPY")
    parser.add_argument("--set", choices=list(SETS), default="price")
    parser.add_argument("--swap", choices=["rates", "fixed"], default="rates", help="スワップの見積もり方")
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

    candidates = SETS[args.set]
    continuous = args.set == "carry"
    if args.swap == "rates":
        rates.fetch_short()
    results: dict[str, dict[str, dict]] = {name: {} for name in [*candidates, *BASELINES, "random"]}
    for symbol in symbols:
        args.symbol = symbol
        bars_symbol = load(symbol)
        if args.swap == "rates":
            bars_symbol = with_swaps(bars_symbol, symbol)
        months = fx_real.by_month(bars_symbol)
        months.pop(current, None)
        names = list(months)
        dev = {m: months[m] for m in names[: -args.lockbox_months]}
        bars_all = pd.concat(months.values())
        warmup = {m: bars_all[(bars_all.index < b.index[0]) & (bars_all.index >= b.index[0] - pd.Timedelta(days=40))]
                  for m, b in dev.items()}
        data_end = str(names[-1])
        config = fx_real.gmo_config(args)
        print(f"{symbol}: 開発期間 {names[0]} 〜 {list(dev)[-1]}（{len(dev)} か月）", flush=True)
        carry = carry_series(symbol, pd.bdate_range(names[0] + "-01", periods=len(names) * 23))
        dev_bars = pd.concat(dev.values())
        for name, (factory, overnight) in {**candidates, **BASELINES}.items():
            make = lambda factory=factory: factory(carry)  # noqa: E731
            if continuous:
                r = run_continuous(dev_bars, make, config, overnight)
            else:
                r = fx_real.run_months(dev, make, config, overnight, warmup)
            results[name][symbol] = r
            if name in candidates:
                experiment_log.append({
                    "kind": "fx_dev", "strategy": name, "label": name, "symbol": symbol, "max_entry_spread": 1.0,
                    "window": [names[0], list(dev)[-1]], "data_end": data_end, "cash": args.cash, "units": args.units,
                    "mean_monthly_return": r["mean_return"], "positive_months": r["positive_months"],
                    "months": r["months"], "trades": r["trades"], "expectancy": r["expectancy"], "t_stat": r["t"],
                })
        if continuous:
            runs = [run_continuous(dev_bars, lambda s=s: fx_real.RANDOM["low_freq"](s), config, False)
                    for s in range(args.random_seeds)]
        else:
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
    for name in candidates:
        mean, up, n, t_all, k = pooled_rows[name]
        expected = expected_max_sharpe(trials, max(k, 2))
        needed = max(2.0, (expected or 0) * math.sqrt(max(k, 2)))
        beat_random = mean > pooled_rows["random"][0]
        verdict = "否定されていない（取り置き期間で確かめる候補）" if (t_all or 0) >= needed and beat_random else "偶然と区別がつかない"
        print(f"  {name:<20} 取引 {k} 件 / t = {t_all or 0:+.2f}（{trials} 通り試せば偶然でも約 {needed:.2f}）→ {verdict}")
    if continuous:
        print("\n【最大の下落（資産の山からの落ち込み）】")
        for name in [*candidates, "buy_and_hold"]:
            print(f"  {name:<20}" + "".join(f"{results[name][s]['max_drawdown']:>+12.1%}" for s in symbols))
    swap_note = "各通貨の短期金利の差から見積もり（取り分0.5%）" if args.swap == "rates" else "全通貨ペアで同じ値を仮定"
    print(f"  ※ スワップは{swap_note}。取り置いた最後の3か月は使っていません")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
