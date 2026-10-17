"""FX の日中売買を、ダミーデータで検証する。**成績の根拠にはならない。仕組みの検証用。**

ダミーデータの値動きの性質は、こちらで決めて作っている（``fx_synthetic``）。
だから、ここで確かめるのは「勝てる戦略か」ではなく、次の3点。

  1. 性質の無いデータ（random）で、どの戦略も「勝てる」と判定されない
  2. 性質を入れたデータで、それに合った戦略が勝ちを拾える
     （meanrev → 逆張り、trend → 順張り）
  3. コストを上げると、その勝ちが消えていく

全部 OK なら「仕組みは壊れていない」。勝てるかどうかは、実際の相場（1万円の運用）で確かめる。

  python scripts/fx_research.py
  python scripts/fx_research.py --seeds 20 --days 20 --units 100
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from jqbt.application.fx_backtest import FxConfig, run_fx
from jqbt.core.fx.model import FxCosts, FxInstrument
from jqbt.infrastructure.fx_synthetic import generate
from jqbt.strategies.fx import BollingerRevert, EmaCross, RandomFx, RangeBreakout

BANNER = "※ ダミーデータの結果です。仕組みの検証であり、実際の相場で勝てることを示すものではありません。"


def strategies(seed: int):
    return [EmaCross(), BollingerRevert(), RangeBreakout(), RandomFx(seed=seed)]


def pooled_t(pnls: list[float]) -> float | None:
    if len(pnls) < 2:
        return None
    a = np.array(pnls)
    s = a.std(ddof=1)
    return float(a.mean() / (s / math.sqrt(len(a)))) if s > 0 else None


def run_grid(regime: str, args, spread: float) -> dict[str, dict]:
    inst = FxInstrument(min_units=args.min_units)
    costs = FxCosts(spread_pips=spread, slippage_pips=args.slippage, stop_slippage_pips=args.stop_slippage)
    config = FxConfig(initial_cash=args.cash, instrument=inst, costs=costs, units=args.units)
    out: dict[str, dict] = {}
    for seed in range(args.seeds):
        bars = generate(days=args.days, regime=regime, seed=seed)
        for strategy in strategies(seed):
            result = run_fx(bars, strategy, config)
            m = result.summary
            name = strategy.name
            row = out.setdefault(name, {"returns": [], "pnls": [], "trades": 0, "losscut": 0, "negative": 0, "skipped": 0})
            row["returns"].append(m["total_return"])
            row["pnls"].extend(t.pnl for t in result.trades)
            row["trades"] += m["trades"]
            row["losscut"] += int(m["losscut"])
            row["negative"] += int(m["negative_balance"])
            row["skipped"] += m["skipped_entries"]
    for row in out.values():
        row["mean_return"] = float(np.mean(row["returns"]))
        row["win_seeds"] = sum(r > 0 for r in row["returns"])
        row["t"] = pooled_t(row["pnls"])
        row["expectancy"] = float(np.mean(row["pnls"])) if row["pnls"] else None
    return out


def table(title: str, rows: dict[str, dict], seeds: int) -> None:
    print(f"\n{title}")
    print(f"  {'戦略':<16}{'平均リターン':>10}{'勝ち越し':>9}{'取引/回':>8}{'1取引の期待値':>12}{'t値':>7}{'ロスカット':>8}{'見送り':>7}")
    for name, r in rows.items():
        t = f"{r['t']:+.2f}" if r["t"] is not None else "—"
        exp = f"{r['expectancy']:+.2f}円" if r["expectancy"] is not None else "—"
        print(f"  {name:<16}{r['mean_return']:>+10.1%}{r['win_seeds']:>5}/{seeds:<3}{r['trades'] / seeds:>8.0f}{exp:>12}{t:>7}"
              f"{r['losscut']:>8}{r['skipped'] // seeds:>7}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--units", type=float, default=100, help="1回の取引の数量（通貨）")
    parser.add_argument("--min-units", type=int, default=1, help="業者の最小取引単位（通貨）")
    parser.add_argument("--days", type=int, default=20, help="1回の検証の営業日数（約1か月）")
    parser.add_argument("--seeds", type=int, default=10, help="ダミーデータを何通り作るか")
    parser.add_argument("--spread", type=float, default=0.2, help="スプレッド（pips）")
    parser.add_argument("--slippage", type=float, default=0.1)
    parser.add_argument("--stop-slippage", type=float, default=0.3)
    args = parser.parse_args()

    print(BANNER)
    print(f"資金 {args.cash:,.0f}円 / 1回 {args.units:,.0f}通貨（最小単位 {args.min_units}）/ スプレッド {args.spread}pips / "
          f"{args.days}営業日 × {args.seeds}通り")

    checks: list[tuple[str, bool, str]] = []

    random_rows = run_grid("random", args, args.spread)
    table("【random】性質なし。どの戦略も勝てないはず", random_rows, args.seeds)
    for name, r in random_rows.items():
        ok = r["t"] is None or r["t"] < 2.0
        checks.append((f"random で {name} が勝ちと判定されない", ok, f"t値 {r['t']:+.2f}" if r["t"] is not None else "取引なし"))

    rows = {}
    for regime, expected, label in (("meanrev", "bollinger_20_2", "逆張り"), ("trend", "ema_cross_5_20", "順張り")):
        rows[regime] = {}
        for spread in (0.0, args.spread, 2.0):
            rows[regime][spread] = run_grid(regime, args, spread)
        table(f"【{regime}】{label}が勝つはず（スプレッド {args.spread}pips）", rows[regime][args.spread], args.seeds)
        free = rows[regime][0.0][expected]
        costly = rows[regime][2.0][expected]
        checks.append((f"{regime} で {expected} が勝ちを拾う（コスト0）", (free["t"] or 0) >= 2.0, f"t値 {free['t'] or 0:+.2f}"))
        checks.append((
            f"{regime} で {expected} の勝ちがコストで減る（0 → 2pips）",
            (costly["expectancy"] or 0) < (free["expectancy"] or 0),
            f"期待値 {free['expectancy'] or 0:+.2f}円 → {costly['expectancy'] or 0:+.2f}円",
        ))
        print(f"  コストの感度（{expected}）: " + " / ".join(
            f"{s}pips: 期待値 {rows[regime][s][expected]['expectancy'] or 0:+.2f}円" for s in (0.0, args.spread, 2.0)))

    jumps = run_grid("jumps", args, args.spread)
    table("【jumps】急変と窓開け。損切り・ロスカットの扱いを見る", jumps, args.seeds)

    print("\n【仕組みの検証】")
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else 'NG'}] {name}（{detail}）")
    passed = all(ok for _, ok, _ in checks)
    print("\n" + ("全て OK: 仕組みは壊れていません。" if passed else "NG があります: 仕組みに問題がある可能性があります。")
          + "勝てるかどうかは、実際の相場で確かめる必要があります。")
    print(BANNER)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
