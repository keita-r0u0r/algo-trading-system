"""FX の判断の段取り（部品）。ジョブ（scripts/fx_paper.py）と見張り役（scripts/fx_watch.py）の両方から使う。

  1. 材料を集める（``build_context``: 値段・足・金利・AI）
  2. 判断する者が、口座（ペーパー / 実際）の状態を見て判断する（``decide``）
  3. 判断を枠（core.fx.frame）に通す
  4. ペーパーなら仮想の約定（``run_paper``）、実際なら発注（``trade_live`` → interfaces.fx_live）

判断の中身（AI・ルール）以外は、すべてここと、その下の部品（core / application / infrastructure）の決まったロジック。
AI は拡張パーツなので、ここでは読み込まず、外から渡す（``build_context(ai=...)``）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import pandas as pd

from ..application.fx_live_trade import Intent, book_from_events
from ..application.fx_paper import (
    PaperAccount,
    PaperPosition,
    advance,
    carry_trend_target,
    execute,
    hourly_breakout_target,
    mark,
)
from ..core.fx.frame import TradingFrame, check
from ..core.fx.model import RISK_PROFILES, SWAP_MARKUP, FxCosts, FxInstrument, swap_per_unit
from ..infrastructure import fx_live_log, fx_paper_log, rates
from ..infrastructure.gmo_fx import GmoFxClient
from ..infrastructure.gmo_fx_store import fetch_day, to_engine
from .fx_live import LiveSession, sync

JST = dt.timezone(dt.timedelta(hours=9))
STATE = Path(os.environ.get("JQBT_STATE_DIR") or Path(__file__).resolve().parents[3] / "state")
KILL = STATE / "KILL_fx_paper"
#: 在れば、この回は判断も仮想の約定もしない（画面の「ペーパー運用を止める」）
PAUSE = STATE / "PAUSE_fx_paper"
#: 回が始まった時刻。見張り役が起こした回と定期の回が重なったら、後のほうは何もしない
STARTED = STATE / "fx_run_started"
#: 見張り役が見る、今の建玉（ペーパーと実際）の要約。小さなファイルにして、毎分読んでも軽くする
POSITIONS = STATE / "fx_positions.json"
OVERLAP_SECONDS = 120
#: AI を呼ぶ間隔（秒）。定期の回は5分ごとだが、AI は見張り役が起こした回か、前回からこの時間たった回だけ呼ぶ
#: （毎回呼ぶと月の費用の上限を超える）。それ以外の回の AI の口座は、前の判断のまま（見送り）
AI_INTERVAL_SECONDS = 15 * 60
AI_LAST = STATE / "fx_ai_last"
BASE = "USD_JPY"
SYMBOLS = ("USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY")
CARRY_ACCOUNTS = {f"carry_trend_{sym}": sym for sym in SYMBOLS}
ACCOUNTS = ("ai", "rule_hourly_breakout", "buy_and_hold", "no_trade", *CARRY_ACCOUNTS)
INITIAL = 10_000.0
#: 前回の実行から間が空いたとき、さかのぼって確かめる最大の日数。これより古い足は見ない（記録に残す）
MAX_GAP_DAYS = 14
#: 金利差＋流れ・買って持つだけの口座は、検証（fx_multi）と同じく、資金が増減しても1回100通貨で持つ。
#: 1万円で損切り150 pips なら、1回の損切りで資金の1.5%。枠は「最大」の度合い（上限2.5%）で確かめる
FIXED_UNITS, CARRY_STOP, HOLD_STOP = 100, 150, 100


#: ルールの口座の売買判断の決まり。判断ごとに、この説明と、そのとき見た数字を記録に残す。
#: 決まりを変えたら RULES_VERSION を上げる（記録を版ごとに分けて比べるため）
RULES_VERSION = "rules-v1"
RULES = {
    "rule_hourly_breakout": "完成した1時間足の終値が、直近20本の高値を上抜けたら買い、安値を下抜けたら売り（ドテン）。"
                            "損切り50 pips、数量は資金の1%（中）から逆算",
    "carry_trend": "前日までの金利差（短期金利の差 − 取り分0.5%）がプラス、かつ前の営業日の終値が50営業日の終値の平均より上なら"
                   "100通貨を買って持つ。どちらかが崩れたら手仕舞う。損切り150 pips",
    "buy_and_hold": "ドル円を100通貨買って持つ。損切り100 pips。損切りに掛かったら、次の回に買い直す",
    "no_trade": "一切売買しない（比べる基準）",
}


def risk_for_units(units: float, stop_pips: float, equity: float, pip: float = 0.01) -> float:
    """決まった数量になるリスクの割合（損切りに掛かったとき、資金の何割を失うか）。"""
    return units * stop_pips * pip / equity if equity > 0 else 0.0
COSTS = FxCosts(slippage_pips=0.1, stop_slippage_pips=0.3, fee_rate=0.00002, losscut_fee=0.05)


def short_rates(today: dt.date) -> dict[str, float]:
    """各通貨の短期金利（%）。今日より前に分かっていた値。1日1回だけ取り込み直す。"""
    root = STATE / "rates"
    stamp = root / "short_fetched_on"
    try:
        if not stamp.exists() or stamp.read_text() != today.isoformat():
            rates.fetch_short(root=root)
            stamp.write_text(today.isoformat())
        day = pd.DatetimeIndex([pd.Timestamp(today)])
        return {c: float(rates.short_rate_known(c, day, root).iloc[0]) for c in rates.SHORT_RATES}
    except Exception:  # 金利が取れなくても、他は進める（スワップ0、金利差の口座は判断しない）
        return {}


def swap_for(symbol: str, price: float, short: dict[str, float]) -> tuple[float, float]:
    base, quote = symbol.split("_")
    if base not in short or quote not in short:
        return 0.0, 0.0
    return swap_per_unit(price, short[base], short[quote], SWAP_MARKUP)


def daily_closes(api: GmoFxClient, symbol: str, now: dt.datetime) -> list[float]:
    """終わった営業日の終値（仲値、古い順、直近2年）。今日のまだ終わっていない日は含めない。"""
    closes: dict[int, float] = {}
    for year in (now.year - 1, now.year):
        bid = {int(b["openTime"]): float(b["close"]) for b in api.klines(symbol, "1day", str(year), "BID")}
        ask = {int(b["openTime"]): float(b["close"]) for b in api.klines(symbol, "1day", str(year), "ASK")}
        closes.update({t: (bid[t] + ask[t]) / 2 for t in bid if t in ask})
    done_before = (now - dt.timedelta(hours=24)).timestamp() * 1000
    return [closes[t] for t in sorted(closes) if t <= done_before]


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
            try:
                raw = fetch_day(api, symbol, day)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                raw = pd.DataFrame()  # その日の営業日がまだ始まっていない（深夜0時〜朝）
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
        "note": "hourly_close は完成した1時間足の仲値の終値（古い順、直近48本）。recent_5min_close は直近2時間の"
                "5分足の終値（古い順、24本）。daily_range_pips は営業日ごとの値幅",
    }
    for sym in frame.symbols:
        if sym not in quotes or sym not in bars:
            continue
        h = hourly(bars[sym], now)
        sessions = bars[sym].groupby("session")
        market["symbols"][sym] = {
            "bid": quotes[sym]["bid"], "ask": quotes[sym]["ask"], "spread_pips": round(quotes[sym]["spread"], 2),
            "hourly_close": [round(float(v), 3) for v in h["close"].tail(48)],
            "recent_5min_close": [round(float(v), 3) for v in
                                  bars[sym]["close"].resample("5min").last().dropna().tail(24)],
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


@dataclass
class Context:
    """1回分の相場の材料。ペーパーの口座と実際の口座が、同じ材料で判断する。"""

    api: GmoFxClient
    now: dt.datetime
    frame: TradingFrame
    carry_frame: TradingFrame
    insts: dict
    quotes: dict
    bars: dict
    ai: object
    today_rates: dict
    short: dict
    #: 同じ状態（建玉なし・同じ向き）なら、AI の判断を使い回す（ペーパーと実際で2回呼ばない）
    ai_cache: dict = field(default_factory=dict)
    closes: dict = field(default_factory=dict)
    #: 見張り役が起こした理由（定期の回なら空）
    trigger: str = ""
    #: この回で AI を呼ぶか
    ai_due: bool = True


def frame_for(name: str, ctx: Context) -> TradingFrame:
    return ctx.carry_frame if name in CARRY_ACCOUNTS or name == "buy_and_hold" else ctx.frame


def position_signature(account: PaperAccount) -> tuple:
    p = account.position
    return () if p is None else (p.symbol, 1 if p.units > 0 else -1)


def decide(name: str, account: PaperAccount, ctx: Context, events: list[dict]) -> list[tuple[dict, str, dict]]:
    """判断する者 ``name`` が、口座 ``account``（ペーパーでも実際でも）の状態を見て判断する。

    返すのは (判断, 理由, 記録に足すもの) の並び。枠（core.fx.frame）はまだ通していない。
    """
    now, quotes, bars, frame = ctx.now, ctx.quotes, ctx.bars, ctx.frame
    decisions: list[tuple[dict, str, dict]] = []
    if name == "ai":
        if ctx.ai is None or not ctx.ai_due:
            return [({"action": "hold"}, "", {"note": "AI を呼ぶ回ではない（前回から15分たっていない・見張り役が起こしていない）"})] \
                if ctx.ai is not None else []
        key = position_signature(account)
        if key in ctx.ai_cache:
            data, extra = ctx.ai_cache[key]
            return [(data, f"AI（同じ状態の判断を使い回し）: {data.get('reasoning', '')}", {**extra, "reused": True})]
        context = ai_context(account, frame, quotes, bars, now, events, ctx.today_rates)
        if ctx.trigger:
            context["market"]["wake_reason"] = f"見張り役がこの回を起こした理由: {ctx.trigger}"
        try:
            reply = ctx.ai.fx_decide(context, now.date())
        except Exception as exc:  # AI が使えなくても、他は進める
            return [({"action": "hold"}, "", {"error": str(exc)[:300]})]
        data = reply.data or {}
        extra = {"model": reply.model, "call_id": reply.call_id, "confidence": data.get("confidence"),
                 "trigger": ctx.trigger,
                 "prompt_version": getattr(ctx.ai, "fx_prompt_version", None),
                 # AI が見たもの。根拠と照らし合わせて、改善の種にする（scripts/fx_paper_review.py）
                 "context": context}
        ctx.ai_cache[key] = (data, extra)
        return [(data, f"AI {reply.model}: {data.get('reasoning', '')}", extra)]
    if name == "rule_hourly_breakout":
        h = hourly(bars[BASE], now)
        want = hourly_breakout_target(h)
        held = 0 if account.position is None else (1 if account.position.units > 0 else -1)
        previous = h.iloc[-21:-1] if len(h) > 20 else h.iloc[0:0]
        logic = {"rule": RULES["rule_hourly_breakout"], "version": RULES_VERSION,
                 "last_close": round(float(h["close"].iloc[-1]), 3) if len(h) else None,
                 "high_20h": round(float(previous["high"].max()), 3) if len(previous) else None,
                 "low_20h": round(float(previous["low"].min()), 3) if len(previous) else None,
                 "target": want, "held": held}
        if want != 0 and want != held:
            if held:
                decisions.append(({"action": "close"}, "1時間足の反対の突破", {"logic": logic}))
            decisions.append(({"action": "open_long" if want > 0 else "open_short", "symbol": BASE,
                               "risk_fraction": frame.risk.risk_per_trade, "stop_pips": 50, "take_pips": 0},
                              "1時間足の突破", {"logic": logic}))
        else:
            decisions.append(({"action": "hold"}, "", {"logic": logic}))
        return decisions
    if name in CARRY_ACCOUNTS:
        sym = CARRY_ACCOUNTS[name]
        base, quote = sym.split("_")
        carry = ctx.short[base] - ctx.short[quote] - SWAP_MARKUP if base in ctx.short and quote in ctx.short else None
        if sym not in ctx.closes:
            ctx.closes[sym] = daily_closes(ctx.api, sym, now)
        closes = ctx.closes[sym]
        want = carry_trend_target(closes, carry)
        logic = {"rule": RULES["carry_trend"], "version": RULES_VERSION, "symbol": sym,
                 "carry_percent": round(carry, 3) if carry is not None else None,
                 "last_close": round(closes[-1], 3) if closes else None,
                 "average_50d": round(sum(closes[-50:]) / 50, 3) if len(closes) >= 50 else None,
                 "target": want, "held": int(account.position is not None)}
        if want == 1 and account.position is None:
            decisions.append(({"action": "open_long", "symbol": sym,
                               "risk_fraction": risk_for_units(FIXED_UNITS, CARRY_STOP, account.cash),
                               "stop_pips": CARRY_STOP, "take_pips": 0},
                              f"金利差 {carry:+.2f}% と上向きの流れ", {"logic": logic}))
        elif want == 0 and account.position is not None:
            decisions.append(({"action": "close"}, "金利差か流れの条件が崩れた", {"logic": logic}))
        else:
            decisions.append(({"action": "hold"}, "", {"logic": logic}))
        return decisions
    if name == "buy_and_hold":
        logic = {"rule": RULES["buy_and_hold"], "version": RULES_VERSION, "held": int(account.position is not None)}
        if account.position is None:
            return [({"action": "open_long", "symbol": BASE,
                      "risk_fraction": risk_for_units(FIXED_UNITS, HOLD_STOP, account.cash),
                      "stop_pips": HOLD_STOP, "take_pips": 0}, "買って持つ", {"logic": logic})]
        return [({"action": "hold"}, "", {"logic": logic})]
    if name == "no_trade":
        return [({"action": "hold"}, "", {"logic": {"rule": RULES["no_trade"], "version": RULES_VERSION}})]
    return []


def build_context(ai, *, trigger: str = "", risk: str = "medium", accounts: dict | None = None) -> "Context | None":
    """1回分の材料を集める。市場が閉まっていれば None。"""
    api = GmoFxClient()
    now = dt.datetime.now(JST).replace(second=0, microsecond=0)
    if api.status() != "OPEN":
        return None
    frame = TradingFrame(symbols=SYMBOLS, risk=RISK_PROFILES[risk])
    carry_frame = TradingFrame(symbols=SYMBOLS, risk=RISK_PROFILES["max"])
    insts = instruments(api)
    quotes = {}
    for t in api.ticker():
        if t["symbol"] in insts:
            bid, ask = float(t["bid"]), float(t["ask"])
            quotes[t["symbol"]] = {"bid": bid, "ask": ask, "spread": (ask - bid) / 0.01}
    # AI には全通貨ペアの直近3日を見せる。建玉のある通貨ペアは、前回の実行から今までの足を全部確かめる
    # （週末や停止のあとに間が空いても、そのあいだの損切りを見逃さない。最大 MAX_GAP_DAYS 日）
    held_since: dict[str, dt.date] = {}
    for a in (accounts or {}).values():
        if a.position is not None and a.last_time:
            day = pd.Timestamp(a.last_time).date()
            held_since[a.position.symbol] = min(day, held_since.get(a.position.symbol, day))
    bars = {}
    for sym in insts:
        gap = (now.date() - held_since[sym]).days + 1 if sym in held_since else 0
        bars[sym] = recent_bars(api, sym, now, min(max(gap, 3), MAX_GAP_DAYS))
    return Context(api=api, now=now, frame=frame, carry_frame=carry_frame, insts=insts, quotes=quotes, bars=bars,
                   ai=ai, today_rates=rates_summary(now.date()) if ai is not None else {},
                   short=short_rates(now.date()), ai_cache={}, trigger=trigger, ai_due=ai_due(trigger))


def run_paper(ctx: "Context", accounts: dict[str, PaperAccount], events: list[dict]) -> list[dict]:
    """ペーパー運用（シミュレーション）を1回進める。新しい出来事を返す（記録は呼び出し側）。"""
    quotes, bars, insts, now = ctx.quotes, ctx.bars, ctx.insts, ctx.now
    new_events: list[dict] = []
    halted_all = KILL.exists()
    for name, account in accounts.items():
        account_frame = frame_for(name, ctx)
        symbol = account.position.symbol if account.position else BASE
        swap_long, swap_short = swap_for(symbol, quotes[symbol]["bid"], ctx.short)
        step = advance(account, bars[symbol], COSTS, insts[symbol], daily_loss_limit=account_frame.risk.daily_loss_limit,
                       swap_long=swap_long, swap_short=swap_short)
        account = step.account
        new_events += step.events
        if account.stopped:
            accounts[name] = account
            continue
        for decision, reason, extra in decide(name, account, ctx, events + new_events):
            equity = mark(account, quotes[account.position.symbol]["bid"], quotes[account.position.symbol]["ask"]) \
                if account.position else account.cash
            approved, rejected = check(
                decision, account_frame, equity=equity, holding=account.position.symbol if account.position else None,
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
    return new_events


def load_paper_accounts() -> tuple[list[dict], dict[str, PaperAccount]]:
    events = fx_paper_log.load()
    snapshots = fx_paper_log.latest_snapshots(events)
    accounts = {name: PaperAccount.from_dict(snapshots[name]) if name in snapshots
                else PaperAccount(name, INITIAL, INITIAL) for name in ACCOUNTS}
    return events, accounts


def claim_run() -> bool:
    """回を始めてよいか（直前に別の回が始まっていたら、重なりを避けて何もしない）。"""
    if STARTED.exists():
        try:
            last = dt.datetime.fromisoformat(STARTED.read_text(encoding="utf-8").strip())
            if (dt.datetime.now(JST) - last).total_seconds() < OVERLAP_SECONDS:
                print(f"直前（{last:%H:%M:%S}）に回が始まっているので、今回は何もしません（重なり防止）")
                return False
        except ValueError:
            pass
    STARTED.parent.mkdir(parents=True, exist_ok=True)
    STARTED.write_text(dt.datetime.now(JST).isoformat(timespec="seconds"), encoding="utf-8")
    return True


#: 見張り役の生きている印。実際の売買をしていて、これより新しければ、ジョブは実際の売買をしない
WATCH_STATE = STATE / "fx_watch.json"
WATCH_FRESH_SECONDS = 5 * 60


def watcher_trades() -> bool:
    """見張り役が実際の売買を担当していて、生きているか。"""
    try:
        state = json.loads(WATCH_STATE.read_text(encoding="utf-8"))
        last = dt.datetime.fromisoformat(state.get("last_cycle") or "")
    except (OSError, ValueError):
        return False
    return bool(state.get("trading")) and (dt.datetime.now(JST) - last).total_seconds() < WATCH_FRESH_SECONDS


def ai_due(trigger: str) -> bool:
    """この回で AI を呼ぶか。見張り役が起こした回は必ず。そうでなければ前回から AI_INTERVAL_SECONDS たったら。"""
    now = dt.datetime.now(JST)
    try:
        last = dt.datetime.fromisoformat(AI_LAST.read_text(encoding="utf-8").strip()) if AI_LAST.exists() else None
    except ValueError:
        last = None
    due = bool(trigger) or last is None or (now - last).total_seconds() >= AI_INTERVAL_SECONDS - 60
    if due:
        try:
            AI_LAST.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
        except OSError:
            pass
    return due


def write_positions(accounts: dict[str, PaperAccount]) -> None:
    """見張り役のための、今の建玉の要約（ペーパーと実際）。"""
    rows = [{"venue": "paper", "account": a.name, "symbol": a.position.symbol, "stop": a.position.stop,
             "take": a.position.take} for a in accounts.values() if a.position is not None]
    rows += [{"venue": "live", "account": p.account, "symbol": p.symbol, "stop": p.stop, "take": p.take}
             for p in book_from_events(fx_live_log.load()).values()]
    try:
        POSITIONS.write_text(json.dumps({"at": dt.datetime.now(JST).isoformat(timespec="seconds"), "positions": rows},
                                        ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"  建玉の要約を書けませんでした: {exc}")


def sync_live() -> None:
    """実際の約定・建玉・資産を読んで記録する（読むだけ。失敗しても止めない）。"""
    try:
        live = sync(quiet=True)
        if live["new"] or live["errors"]:
            print(f"  実際の約定: 新しく {live['new']} 件" + (f"（読めなかったもの {len(live['errors'])} 件）" if live["errors"] else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"  実際の約定を読めませんでした: {type(exc).__name__}")


def trade_live(ctx: "Context") -> None:
    """実際の口座で、判断する者ごとに、実際の建玉を見て判断し、発注する。"""
    session = LiveSession.open()
    if session is None:
        return
    intents: list[Intent] = []
    for name in session.config.accounts:
        view = live_view(name, session)
        account_frame = frame_for(name, ctx)
        for decision, reason, extra in decide(name, view, ctx, []):
            equity = session.equity
            approved, rejected = check(
                decision, account_frame, equity=equity, holding=view.position.symbol if view.position else None,
                spread_pips={s: q["spread"] for s, q in ctx.quotes.items()},
                prices={s: q["ask"] for s, q in ctx.quotes.items()}, instruments=ctx.insts, halted=False,
            )
            session.events.append({"kind": "live_decision", "at": ctx.now.isoformat(), "account": name,
                                   "decision": {k: v for k, v in decision.items() if k != "context"},
                                   "approved": approved is not None, "rejected": rejected,
                                   **{k: v for k, v in extra.items() if k in ("logic", "model", "prompt_version",
                                                                             "confidence", "reused")}})
            if approved is None:
                continue
            intents.append(Intent(account=name, action="close" if approved.action == "close" else "open",
                                  at=ctx.now.isoformat(), symbol=approved.symbol, units=approved.units,
                                  stop_pips=approved.stop_pips, take_pips=approved.take_pips, reason=reason))
    session.execute(intents)
    session.finish()


def live_view(name: str, session) -> PaperAccount:
    """実際の口座を、判断する者が見る形にする（建玉は、このシステムがその判断する者のために建てたもの）。"""
    p = session.position_of(name)
    position = None
    if p is not None:
        sign = 1 if p.side == "BUY" else -1
        position = PaperPosition(p.symbol, sign * p.size, float(p.price or 0), p.stop, p.take, p.opened_at, "実際の建玉")
    equity = session.equity
    return PaperAccount(name, equity, equity, position, equity=equity, day_start_equity=equity)

