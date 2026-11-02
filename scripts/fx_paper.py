"""FX の前向きのペーパー運用を1回進める。**注文は出さない**（仮想の約定だけ）。

  python scripts/fx_paper.py            # 1回進める（Cloud Run では定期的に自動で呼ばれる。今は15分ごと）
  python scripts/fx_paper.py --status   # 口座の状態を見るだけ

口座は判断する者ごとに分かれ、同じ時刻・同じ値段で並ぶ。

| 口座 | 判断 |
|---|---|
| ``ai`` | AI（拡張パーツ）。枠の中で、通貨ペア・向き・リスクの割合・損切り・利確を自由に決める |
| ``rule_hourly_breakout`` | 1時間足の突破（``HourlyBreakout`` と同じ決まり） |
| ``buy_and_hold`` | ドル円を買って持つ（損切り100 pips。掛かったら次の回に買い直す） |
| ``no_trade`` | 一切売買しない |
| ``carry_trend_<通貨ペア>`` | 金利差＋流れ（``CarryFilter`` の trend と同じ決まり）を4通貨ペアで。過去の検証で一番有力だった候補を、
  同じ数値のまま新しいデータで確かめる。1回100通貨・損切り150 pips（検証と同じ大きさ） |

スワップは、各通貨の短期金利の差から見積もる（検証と同じ。取り分0.5%）。

AI を使うには、拡張パーツ ``ai`` を有効にする（``JQBT_EXTENSIONS=ai``）。無ければ AI の口座は飛ばす。
枠（``core.fx.frame``）を外れた判断は実行せず、理由と一緒に記録する。
記録は ``state/fx_paper.jsonl``。キルスイッチは ``state/KILL_fx_paper``（在れば新規を作らない）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.core.fx.model import RISK_PROFILES  # noqa: E402
from jqbt.infrastructure import fx_paper_log  # noqa: E402
from jqbt.infrastructure.env import load_env  # noqa: E402
from jqbt.interfaces import fx_runtime as rt  # noqa: E402
from jqbt.interfaces.extensions import enabled_names  # noqa: E402


def build_ai():
    """AI（拡張パーツ）。有効なときだけ読む。部品（interfaces）は拡張パーツを読まないので、ここで作って渡す。"""
    if "ai" not in enabled_names():
        return None
    from jqbt.extensions.ai import build_assistant

    return build_assistant()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="口座の状態を見るだけ（進めない）")
    parser.add_argument("--risk", choices=list(RISK_PROFILES), default=os.environ.get("FX_PAPER_RISK", "medium"))
    args = parser.parse_args()
    load_env()

    events, accounts = rt.load_paper_accounts()
    if args.status:
        for acc in accounts.values():
            print(f"{acc.name:<22} 評価額 {acc.equity or acc.cash:>9,.0f}円  建玉 "
                  f"{(acc.position.symbol + ' ' + f'{acc.position.units:+,.0f}') if acc.position else '—'}")
        return 0

    trigger = os.environ.get("TRIGGER_REASON", "")
    if not rt.claim_run():
        return 0
    paused = rt.PAUSE.exists()
    live_here = not rt.watcher_trades()  # 見張り役が実際の売買をしていれば、ジョブはペーパーだけ
    ctx = rt.build_context(build_ai(), trigger=trigger, risk=args.risk, accounts=accounts if not paused else {})
    if ctx is None:
        print("市場が閉まっています。今回は進めません")
        if live_here:
            rt.sync_live()
        return 0

    new_events: list[dict] = []
    if paused:
        print(f"ペーパー運用は止めています（{rt.PAUSE.read_text(encoding='utf-8').strip()}）。実際の売買は別に動きます")
    else:
        new_events = rt.run_paper(ctx, accounts, events)
        fx_paper_log.append(new_events)

    if live_here:
        try:
            rt.trade_live(ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"  自動売買でエラー: {type(exc).__name__}: {str(exc)[:200]}")
        rt.sync_live()
    else:
        print("実際の売買は見張り役が行っています（ジョブはペーパー運用だけ）")
    rt.write_positions(accounts if not paused else {})

    print(f"{ctx.now:%Y-%m-%d %H:%M} 日本時間 / リスクの度合い {args.risk}" + (f" / 起こした理由: {trigger}" if trigger else ""))
    for e in new_events:
        if e["kind"] == "decision":
            mark_ = "通過" if e.get("approved") else ("見送り" if not e.get("rejected") else f"却下: {e['rejected']}")
            print(f"  [{e['account']}] 判断 {(e.get('decision') or {}).get('action', '—')} → {mark_}")
        elif e["kind"] in ("open", "close", "halted", "stopped"):
            detail = f"{e.get('symbol', '')} {e.get('units', 0):+,.0f} @ {e.get('price', e.get('exit_price', ''))}"
            pnl = f" 損益 {e['pnl']:+,.0f}円" if "pnl" in e else ""
            print(f"  [{e['account']}] {e['kind']} {detail}{pnl} {e.get('reason', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
