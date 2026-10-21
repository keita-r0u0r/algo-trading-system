"""FX の前向きのペーパー運用を1回進める。**注文は出さない**（仮想の約定だけ）。

  python scripts/fx_paper.py            # 1回進める（Cloud Run では1時間に1回、自動で呼ばれる）
  python scripts/fx_paper.py --status   # 口座の状態を見るだけ

口座は判断する者ごとに分かれ、同じ時刻・同じ値段で並ぶ。

| 口座 | 判断 |
|---|---|
| ``ai`` | AI（拡張パーツ）。枠の中で、通貨ペア・向き・リスクの割合・損切り・利確を自由に決める |
| ``rule_hourly_breakout`` | 1時間足の突破（``HourlyBreakout`` と同じ決まり） |
| ``buy_and_hold`` | ドル円を買って持つ（損切り100 pips。掛かったら次の回に買い直す） |
| ``no_trade`` | 一切売買しない |

AI を使うには、拡張パーツ ``ai`` を有効にする（``JQBT_EXTENSIONS=ai``）。無ければ AI の口座は飛ばす。
枠（``core.fx.frame``）を外れた判断は実行せず、理由と一緒に記録する。
記録は ``state/fx_paper.jsonl``。キルスイッチは ``state/KILL_fx_paper``（在れば新規を作らない）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from jqbt.application.fx_paper import (  # noqa: E402
    PaperAccount,
    advance,
    execute,
    hourly_breakout_target,
    mark,
)
from jqbt.core.fx.frame import TradingFrame, check  # noqa: E402
from jqbt.core.fx.model import RISK_PROFILES, FxCosts, FxInstrument  # noqa: E402
from jqbt.infrastructure import fx_paper_log, rates  # noqa: E402
from jqbt.infrastructure.env import load_env  # noqa: E402
from jqbt.infrastructure.gmo_fx import GmoFxClient  # noqa: E402
from jqbt.infrastructure.gmo_fx_store import fetch_day, to_engine  # noqa: E402
from jqbt.interfaces.extensions import enabled_names  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
STATE = Path(__file__).resolve().parents[1] / "state"
KILL = STATE / "KILL_fx_paper"
BASE = "USD_JPY"
SYMBOLS = ("USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY")
ACCOUNTS = ("ai", "rule_hourly_breakout", "buy_and_hold", "no_trade")
INITIAL = 10_000.0
#: スワップ（円/通貨/日）。**仮定**（fx_real.py と同じ）。日をまたいで持つ建玉にだけ効く
SWAP_LONG, SWAP_SHORT = 0.012, -0.013
COSTS = FxCosts(slippage_pips=0.1, stop_slippage_pips=0.3, fee_rate=0.00002, losscut_fee=0.05)


def instruments(api: GmoFxClient) -> dict[str, FxInstrument]:
    specs = {s["symbol"]: s for s in api.symbols()}
    return {
        sym: FxInstrument(symbol=sym, pip=0.01, min_units=int(float(specs[sym]["minOpenOrderSize"])),
                          leverage=25, losscut_ratio=1.0, entry_leverage=20)
        for sym in SYMBOLS if sym in specs
    }


def recent_bars(api: GmoFxClient, symbol: str, now: dt.datetime, days_back: int) -> pd.DataFrame:
    """直近の営業日の1分足（仲値・spread・session）。週末は飛ばす。"""
    frames = []
    day = now.date() - dt.timedelta(days=days_back)
    while day <= now.date():
        if day.weekday() < 5:
            raw = fetch_day(api, symbol, day)
            if not raw.empty:
                raw["session"] = day
                frames.append(raw)
        day += dt.timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "spread", "session"])
    bars = to_engine(pd.concat(frames))
    return bars[bars.index <= now.replace(tzinfo=None)]


def hourly(bars: pd.DataFrame, now: dt.datetime) -> pd.DataFrame:
    """完成した1時間足（今の1時間は含めない）。"""
    current_hour = pd.Timestamp(now.replace(tzinfo=None)).floor("h")
    done = bars[bars.index < current_hour]
    return done.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def rates_summary(today: dt.date) -> dict:
    """日米の2年金利（今日より前に分かっていた値）。1日1回だけ取り込み直す。"""
    root = STATE / "rates"
    try:
        stamp = root / "fetched_on"
        if not stamp.exists() or stamp.read_text() != today.isoformat():
            rates.fetch(root=root)
            stamp.write_text(today.isoformat())
        spread = rates.known_before(rates.spread(root=root), today)
        us = rates.known_before(rates.load("us2y", root), today)
        jp = rates.known_before(rates.load("jp2y", root), today)
        return {
            "us_2y": round(float(us.iloc[-1]), 3), "jp_2y": round(float(jp.iloc[-1]), 3),
            "us_minus_jp_2y": round(float(spread.iloc[-1]), 3),
            "change_20d": round(float(spread.iloc[-1] - spread.iloc[-21]), 3) if len(spread) > 21 else None,
            "as_of": str(spread.index[-1].date()),
        }
    except Exception as exc:  # 金利が取れなくても、他の判断は進める
        return {"error": f"金利を取り込めませんでした（{type(exc).__name__}）"}


def ai_context(account: PaperAccount, frame: TradingFrame, quotes: dict, bars: dict, now: dt.datetime,
               events: list[dict], today_rates: dict) -> dict:
    p = account.position
    closes = [e for e in events if e.get("kind") == "close" and e.get("account") == account.name][-10:]
    market = {
        "now_jst": now.strftime("%Y-%m-%d %H:%M (%a)"),
        "rates_2y_percent": today_rates,
        "symbols": {},
        "note": "hourly_close は完成した1時間足の仲値の終値（古い順、直近48本）。daily_range_pips は営業日ごとの値幅",
    }
    for sym in frame.symbols:
        if sym not in quotes or sym not in bars:
            continue
        h = hourly(bars[sym], now)
        sessions = bars[sym].groupby("session")
        market["symbols"][sym] = {
            "bid": quotes[sym]["bid"], "ask": quotes[sym]["ask"], "spread_pips": round(quotes[sym]["spread"], 2),
            "hourly_close": [round(float(v), 3) for v in h["close"].tail(48)],
            "daily_range_pips": [round(float((g["high"].max() - g["low"].min()) / 0.01), 1) for _, g in sessions][-5:],
        }
    return {
        "frame": frame.describe(),
        "account": {
            "cash": round(account.cash, 1),
            "equity": round(mark(account, quotes[p.symbol]["bid"], quotes[p.symbol]["ask"]) if p else account.cash, 1),
            "day_start_equity": round(account.day_start_equity or account.cash, 1),
            "halted_today": account.halted_session == account.session,
            "position": None if p is None else {
                "symbol": p.symbol, "side": "long" if p.units > 0 else "short", "units": p.units,
                "entry_price": p.price, "stop": p.stop, "take": p.take, "opened_at": p.opened_at,
            },
            "recent_closed_trades": [
                {"symbol": e["symbol"], "pnl_yen": round(e["pnl"], 1), "reason": e["reason"], "closed_at": e["at"]}
                for e in closes
            ],
        },
        "market": market,
    }


def build_ai():
    if "ai" not in enabled_names():
        return None
    from jqbt.extensions.ai import build_assistant  # 拡張パーツ（有効なときだけ読む）

    return build_assistant()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="口座の状態を見るだけ（進めない）")
    parser.add_argument("--risk", choices=list(RISK_PROFILES), default=os.environ.get("FX_PAPER_RISK", "medium"))
    args = parser.parse_args()
    load_env()

    events = fx_paper_log.load()
    snapshots = fx_paper_log.latest_snapshots(events)
    accounts = {name: PaperAccount.from_dict(snapshots[name]) if name in snapshots
                else PaperAccount(name, INITIAL, INITIAL) for name in ACCOUNTS}
    if args.status:
        for acc in accounts.values():
            print(f"{acc.name:<22} 評価額 {acc.equity or acc.cash:>9,.0f}円  建玉 "
                  f"{(acc.position.symbol + ' ' + f'{acc.position.units:+,.0f}') if acc.position else '—'}")
        return 0

    api = GmoFxClient()
    now = dt.datetime.now(JST).replace(second=0, microsecond=0)
    status = api.status()
    if status != "OPEN":
        print(f"市場の状態: {status}。今回は進めません")
        return 0

    frame = TradingFrame(symbols=SYMBOLS, risk=RISK_PROFILES[args.risk])
    insts = instruments(api)
    quotes = {}
    for t in api.ticker():
        if t["symbol"] in insts:
            bid, ask = float(t["bid"]), float(t["ask"])
            quotes[t["symbol"]] = {"bid": bid, "ask": ask, "spread": (ask - bid) / 0.01}
    # 1日の損失や、前回からの損切りの確認に要る分（最大4日前まで）。AI には全通貨ペアの直近を見せる
    oldest = min((pd.Timestamp(a.last_time).date() for a in accounts.values() if a.last_time), default=now.date())
    days_back = min(max((now.date() - oldest).days + 1, 3), 4)
    bars = {sym: recent_bars(api, sym, now, days_back) for sym in insts}
    halted_all = KILL.exists()

    new_events: list[dict] = []
    ai = build_ai()
    today_rates = rates_summary(now.date()) if ai is not None else {}
    for name, account in accounts.items():
        symbol = account.position.symbol if account.position else BASE
        step = advance(account, bars[symbol], COSTS, insts[symbol], daily_loss_limit=frame.risk.daily_loss_limit,
                       swap_long=SWAP_LONG, swap_short=SWAP_SHORT)
        account = step.account
        new_events += step.events
        if account.stopped:
            accounts[name] = account
            continue

        decisions: list[tuple[dict, str, dict]] = []  # (判断, 理由, 記録に足すもの)
        if name == "ai":
            if ai is None:
                accounts[name] = account
                continue
            context = ai_context(account, frame, quotes, bars, now, events + new_events, today_rates)
            try:
                reply = ai.fx_decide(context, now.date())
            except Exception as exc:  # AI が使えなくても、他の口座は進める
                new_events.append({"kind": "decision", "account": name, "at": now.isoformat(), "error": str(exc)[:300]})
                accounts[name] = account
                continue
            data = reply.data or {}
            decisions.append((data, f"AI {reply.model}: {data.get('reasoning', '')}",
                              {"model": reply.model, "call_id": reply.call_id, "confidence": data.get("confidence"),
                               # AI が見たもの。根拠と照らし合わせて、改善の種にする（scripts/fx_paper_review.py）
                               "context": context}))
        elif name == "rule_hourly_breakout":
            want = hourly_breakout_target(hourly(bars[BASE], now))
            held = 0 if account.position is None else (1 if account.position.units > 0 else -1)
            if want != 0 and want != held:
                if held:
                    decisions.append(({"action": "close"}, "1時間足の反対の突破", {}))
                decisions.append(({"action": "open_long" if want > 0 else "open_short", "symbol": BASE,
                                   "risk_fraction": frame.risk.risk_per_trade, "stop_pips": 50, "take_pips": 0},
                                  "1時間足の突破", {}))
        elif name == "buy_and_hold" and account.position is None:
            decisions.append(({"action": "open_long", "symbol": BASE, "risk_fraction": frame.risk.risk_per_trade,
                               "stop_pips": 100, "take_pips": 0}, "買って持つ", {}))

        for decision, reason, extra in decisions:
            equity = mark(account, quotes[account.position.symbol]["bid"], quotes[account.position.symbol]["ask"]) \
                if account.position else account.cash
            approved, rejected = check(
                decision, frame, equity=equity, holding=account.position.symbol if account.position else None,
                spread_pips={s: q["spread"] for s, q in quotes.items()}, prices={s: q["ask"] for s, q in quotes.items()},
                instruments=insts, halted=halted_all or account.halted_session == account.session,
            )
            new_events.append({"kind": "decision", "account": name, "at": now.isoformat(), "decision": decision,
                               "approved": approved is not None, "rejected": rejected, **extra})
            if approved is None:
                continue
            q = quotes[approved.symbol]
            step = execute(account, approved, bid=q["bid"], ask=q["ask"], at=now, costs=COSTS,
                           inst=insts[approved.symbol], reason=reason)
            account = step.account
            new_events += step.events
        accounts[name] = account

    for name, account in accounts.items():
        p = account.position
        equity = mark(account, quotes[p.symbol]["bid"], quotes[p.symbol]["ask"]) if p else account.cash
        accounts[name] = account = replace(account, equity=equity)
        new_events.append({"kind": "snapshot", "account": name, "at": now.isoformat(), "state": account.to_dict()})
    fx_paper_log.append(new_events)

    print(f"{now:%Y-%m-%d %H:%M} 日本時間 / リスクの度合い {args.risk}（ペーパー運用。注文は出していません）")
    for e in new_events:
        if e["kind"] == "decision":
            mark_ = "通過" if e.get("approved") else ("見送り" if not e.get("rejected") else f"却下: {e['rejected']}")
            action = (e.get("decision") or {}).get("action", "—") if "error" not in e else f"エラー: {e['error']}"
            print(f"  [{e['account']}] 判断 {action} → {mark_}")
        elif e["kind"] in ("open", "close", "halted", "stopped"):
            detail = f"{e.get('symbol', '')} {e.get('units', 0):+,.0f} @ {e.get('price', e.get('exit_price', ''))}"
            pnl = f" 損益 {e['pnl']:+,.0f}円" if "pnl" in e else ""
            print(f"  [{e['account']}] {e['kind']} {detail}{pnl} {e.get('reason', '')}")
    for acc in accounts.values():
        print(f"  {acc.name:<22} 評価額 {acc.equity:>9,.0f}円")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
