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

from ..application import fx_calendar, fx_exits, fx_features, fx_filter, fx_setups
from ..application.fx_live_trade import Intent, LiveConfig, book_from_events
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
from ..infrastructure import econ_calendar, fx_live_log, fx_paper_log, rates, typesafe
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
#: 実際の口座で、金利差＋流れの数量を満額にする流れの強さ（終値が50営業日の平均を上回る割合）と、最小の割合
CARRY_FULL_TREND, CARRY_MIN_SCALE = 0.02, 0.3


def carry_scale(closes: list[float]) -> float:
    """流れの強さ（0.3〜1）。終値が50営業日の平均を CARRY_FULL_TREND 上回れば1。"""
    if len(closes) < 50:
        return CARRY_MIN_SCALE
    average = sum(closes[-50:]) / 50
    return min(1.0, max(CARRY_MIN_SCALE, (closes[-1] / average - 1) / CARRY_FULL_TREND))


#: ルールの口座の売買判断の決まり。判断ごとに、この説明と、そのとき見た数字を記録に残す。
#: 決まりを変えたら RULES_VERSION を上げる（記録を版ごとに分けて比べるため）
RULES_VERSION = "rules-v2"
RULES = {
    "rule_hourly_breakout": "完成した1時間足の終値が、直近20本の高値を上抜けたら買い、安値を下抜けたら売り（ドテン）。"
                            "損切り50 pips、数量は資金の1%（中）から逆算",
    "carry_trend": "前日までの金利差（短期金利の差 − 取り分0.5%）がプラス、かつ前の営業日の終値が50営業日の終値の平均より上なら"
                   "買って持つ。どちらかが崩れたら手仕舞う。損切り150 pips。数量は、ペーパーでは100通貨。"
                   "実際の口座では、流れの強さ（終値が平均を何%上回るか。2%以上で満額）に応じて、"
                   "資金の最大5%（30%以上）のリスクから逆算する",
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


#: 終わった営業日の1分足（プロセスの中でだけ覚える）
_DAYS: dict[tuple[str, dt.date], pd.DataFrame] = {}
#: 1分足をさかのぼる最小の日数。ハーネスの1時間足の50本平均に、週末をはさんでも足りるように
BARS_DAYS = 5


def recent_bars(api: GmoFxClient, symbol: str, now: dt.datetime, days_back: int) -> pd.DataFrame:
    """直近の営業日の1分足（仲値・spread・session）。週末は飛ばす。"""
    frames = []
    day = now.date() - dt.timedelta(days=days_back)
    while day <= now.date():
        if day.weekday() < 5:
            if (symbol, day) in _DAYS:
                raw = _DAYS[(symbol, day)].copy()
            else:
                try:
                    raw = fetch_day(api, symbol, day)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    raw = pd.DataFrame()  # その日の営業日がまだ始まっていない（深夜0時〜朝）
                if not raw.empty and day < now.date() - dt.timedelta(days=1):
                    _DAYS[(symbol, day)] = raw.copy()  # 終わった営業日は変わらないので覚える（見張り役は動き続ける）
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


def position_numbers(p, quote: dict | None, pip: float = 0.01) -> dict:
    """建玉の数字を、AI が計算しなくて済むように出す（AI は生の値段から pips や円を読み違えるので）。

    決済するときの値段は、買いなら bid、売りなら ask。円の損益は 数量 × 値幅（円）。
    """
    sign = 1 if p.units > 0 else -1
    numbers = {"symbol": p.symbol, "side": "long" if sign > 0 else "short", "units": abs(p.units),
               "entry_price": p.price, "stop": p.stop, "take": p.take, "opened_at": p.opened_at,
               "yen_per_pip": round(abs(p.units) * pip, 2)}
    if quote and p.price:
        exit_price = quote["bid"] if sign > 0 else quote["ask"]
        numbers["exit_price_now"] = exit_price
        numbers["unrealized_pips"] = round(sign * (exit_price - p.price) / pip, 1)
        numbers["unrealized_yen"] = round(sign * (exit_price - p.price) * abs(p.units), 1)
        if p.stop:
            numbers["pips_to_stop"] = round(abs(exit_price - p.stop) / pip, 1)
        if p.take:
            numbers["pips_to_take"] = round(abs(p.take - exit_price) / pip, 1)
    return numbers


def plan_when_opened(name: str, position, events: list[dict], now: dt.datetime) -> dict | None:
    """今の建玉を建てたときの、判断する者の計画（根拠・閉じる条件・保有の目安）。記録から探す。

    AI は呼ばれるたびにゼロから考え直すので、渡さないと、15分の細かい揺れで建ててすぐ閉じてしまう（コスト負け）。
    建玉の時刻より前で、同じ通貨ペアを建てると決めて通った、いちばん新しい判断を使う。
    """
    if position is None:
        return None
    try:
        opened = dt.datetime.fromisoformat(str(position.opened_at))
    except ValueError:
        opened = None
    for e in reversed(events):
        d = e.get("decision") or {}
        if (e.get("kind") in ("decision", "live_decision") and e.get("account") == name and e.get("approved")
                and str(d.get("action", "")).startswith("open") and d.get("symbol") == position.symbol):
            try:
                at = dt.datetime.fromisoformat(str(e.get("at")))
            except ValueError:
                continue
            if opened is not None and at > opened:
                continue
            start = opened or at
            return {"opened_at": start.isoformat(timespec="minutes"),
                    "minutes_held": int((now - start).total_seconds() // 60),
                    "reasoning": d.get("reasoning", ""), "exit_condition": d.get("exit_condition", ""),
                    "hold_minutes": d.get("hold_minutes"), "confidence": d.get("confidence"),
                    "stop_pips": d.get("stop_pips"), "take_pips": d.get("take_pips"),
                    "setup": d.get("setup"), "exit_method": d.get("exit_method"),
                    "invalidation_price": d.get("invalidation_price") or None,
                    "target_price": d.get("target_price") or None}
    return None


def ai_context(account: PaperAccount, frame: TradingFrame, quotes: dict, bars: dict, now: dt.datetime,
               events: list[dict], today_rates: dict, daily: dict | None = None) -> dict:
    p = account.position
    closes = [e for e in events if e.get("kind") == "close" and e.get("account") == account.name][-10:]
    market = {
        "now_jst": now.strftime("%Y-%m-%d %H:%M (%a)"),
        "rates_2y_percent": today_rates,
        "symbols": {},
        "note": "features は1分足から計算した特徴量（application/fx_features.py）。summary に同じ数字の文がある。"
                "moves.*.noise_ratio は、その長さの動きがふだんの揺れの何倍か（1未満は揺れの範囲で、流れの根拠にならない）。"
                "features.recent_1min_close は直近30分の1分足の終値。hourly_close は完成した1時間足の終値（直近24本）。"
                "recent_5min_close は直近2時間の5分足の終値（24本）。daily_range_pips は営業日ごとの値幅。円の通貨ペアでは 1 pip = 0.01円。"
                "建玉の含み損益・損切りまでの距離は account.position に計算済み（自分で計算し直さない）",
    }
    for sym in frame.symbols:
        if sym not in quotes or sym not in bars:
            continue
        h = hourly(bars[sym], now)
        sessions = bars[sym].groupby("session")
        market["symbols"][sym] = {
            "bid": quotes[sym]["bid"], "ask": quotes[sym]["ask"], "spread_pips": round(quotes[sym]["spread"], 2),
            "features": fx_features.market_features(bars[sym], now, daily_closes=(daily or {}).get(sym)),
            "hourly_close": [round(float(v), 3) for v in h["close"].tail(24)],
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
                **position_numbers(p, quotes.get(p.symbol)),
                "plan_when_opened": plan_when_opened(account.name, p, events, now)
                or "記録が無い（計画を残す前に建てた建玉）。損切り・利確に任せるのを基本にする",
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
    #: 実際の口座の枠（利用者が決めた: 1回の損切りで資金の最大5%・数量は状況に応じて上限1,000通貨）
    live_frame: TradingFrame
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
    #: この回の前に、最後に AI を呼んだ時刻（呼ばない理由を記録に残すため）
    ai_last: dt.datetime | None = None
    #: 経済指標の予定（application/fx_calendar.py。取れなければ空）
    calendar: list = field(default_factory=list)
    #: 通貨ペアごとの日足の終値（特徴量の日足の部分に使う。1日1回だけ取る）
    daily: dict = field(default_factory=dict)
    #: 画面の設定（判断する者の ON/OFF と、見送りを聞く相手）。state/fx_settings.json
    settings: fx_filter.FxSettings = field(default_factory=fx_filter.FxSettings)
    #: Jev の呼び出し口（キーが無い・使わない設定なら None）
    jev: object = None
    #: 通貨ペアごとの特徴量（この回の中で使い回す）
    features: dict = field(default_factory=dict)


def frame_for(name: str, ctx: Context) -> TradingFrame:
    return ctx.carry_frame if name in CARRY_ACCOUNTS or name == "buy_and_hold" else ctx.frame


def position_signature(account: PaperAccount) -> tuple:
    p = account.position
    return () if p is None else (p.symbol, 1 if p.units > 0 else -1)


def decide(name: str, account: PaperAccount, ctx: Context, events: list[dict], *,
           live: bool = False) -> list[tuple[dict, str, dict]]:
    """判断する者 ``name`` が、口座 ``account``（ペーパーでも実際でも）の状態を見て判断する。

    返すのは (判断, 理由, 記録に足すもの) の並び。枠（core.fx.frame）はまだ通していない。
    ``live`` なら実際の口座の枠で判断する（数量は状況に応じて決める）。
    """
    now, quotes, bars = ctx.now, ctx.quotes, ctx.bars
    frame = ctx.live_frame if live else ctx.frame
    decisions: list[tuple[dict, str, dict]] = []
    if name == "ai":
        if ctx.ai is None or not ctx.ai_due:
            return [({"action": "hold"}, "", {"note": ai_skip_reason(ctx)})] if ctx.ai is not None else []
        key = (live, position_signature(account))  # 枠が違えば判断も違うので、ペーパーと実際では使い回さない
        if key in ctx.ai_cache:
            data, extra = ctx.ai_cache[key]
            return [(data, f"AI（同じ状態の判断を使い回し）: {data.get('reasoning', '')}", {**extra, "reused": True})]
        context = ai_context(account, frame, quotes, bars, now, events, ctx.today_rates, ctx.daily)
        if live:
            context["frame"]["max_units"] = LiveConfig().max_units
            context["goal"] = ("実際の口座。利用者の目標は1か月で資金1.5倍。数量は risk_fraction（枠の上限まで）で決まる。"
                               "根拠が強いときは大きく、弱いときは小さく、無いときは hold。枠と損切りは必ず守る")
        context["market"]["economic_calendar"] = {
            "events": fx_calendar.upcoming(ctx.calendar, now, frame.symbols),
            "note": "この先12時間・直前2時間の、通貨ペアに関係する経済指標の発表（重要度は中以上。時刻は日本時間。"
                    "minutes_from_now が負なら発表済み）。予定が取れなかったときは空",
        }
        notes = active_notes(now)
        if notes:
            context["user_notes"] = [{"at": n["at"][5:16].replace("T", " "), "until": n["until"][5:16].replace("T", " "),
                                      "text": n["text"]} for n in notes]
        if ctx.trigger:
            context["market"]["wake_reason"] = f"見張り役がこの回を起こした理由: {ctx.trigger}"
        try:
            reply = ctx.ai.fx_decide(context, now.date())
        except Exception as exc:  # AI が使えなくても、他は進める（理由は記録に残す）
            print(f"AI を呼べなかった: {type(exc).__name__}: {str(exc)[:300]}", flush=True)
            return [({"action": "hold"}, "", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})]
        data = reply.data or {}
        extra = {"model": reply.model, "call_id": reply.call_id, "confidence": data.get("confidence"),
                 "trigger": ctx.trigger,
                 "prompt_version": getattr(ctx.ai, "fx_prompt_version", None),
                 # AI が見たもの。根拠と照らし合わせて、改善の種にする（scripts/fx_paper_review.py）
                 "context": context}
        ctx.ai_cache[key] = (data, extra)
        return [(data, f"AI {reply.model}: {data.get('reasoning', '')}", extra)]
    if name == "harness":
        return [harness_decision(account, ctx, events)]
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
        scale = carry_scale(closes)
        risk_fraction = (ctx.live_frame.risk.risk_per_trade * scale if live
                         else risk_for_units(FIXED_UNITS, CARRY_STOP, account.cash))
        logic = {"rule": RULES["carry_trend"], "version": RULES_VERSION, "symbol": sym,
                 "carry_percent": round(carry, 3) if carry is not None else None,
                 "last_close": round(closes[-1], 3) if closes else None,
                 "average_50d": round(sum(closes[-50:]) / 50, 3) if len(closes) >= 50 else None,
                 "target": want, "held": int(account.position is not None),
                 "trend_scale": round(scale, 2), "risk_fraction": round(risk_fraction, 4)}
        if want == 1 and account.position is None:
            decisions.append(({"action": "open_long", "symbol": sym,
                               "risk_fraction": risk_fraction,
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
    live_frame = TradingFrame(symbols=SYMBOLS, risk=RISK_PROFILES["live"])
    insts = instruments(api)
    quotes = {}
    for t in api.ticker():
        if t["symbol"] in insts:
            bid, ask = float(t["bid"]), float(t["ask"])
            quotes[t["symbol"]] = {"bid": bid, "ask": ask, "spread": (ask - bid) / 0.01}
    # 全通貨ペアの直近 BARS_DAYS 日を見る。建玉のある通貨ペアは、前回の実行から今までの足を全部確かめる
    # （週末や停止のあとに間が空いても、そのあいだの損切りを見逃さない。最大 MAX_GAP_DAYS 日）
    held_since: dict[str, dt.date] = {}
    for a in (accounts or {}).values():
        if a.position is not None and a.last_time:
            day = pd.Timestamp(a.last_time).date()
            held_since[a.position.symbol] = min(day, held_since.get(a.position.symbol, day))
    bars = {}
    for sym in insts:
        gap = (now.date() - held_since[sym]).days + 1 if sym in held_since else 0
        bars[sym] = recent_bars(api, sym, now, min(max(gap, BARS_DAYS), MAX_GAP_DAYS))
    settings = load_settings()
    uses_jev = (settings.filter.mode != "off" and settings.filter.provider == "jev") or settings.exits.selector == "jev"
    jev = typesafe.jev_from_secrets() if uses_jev else None
    return Context(api=api, now=now, frame=frame, carry_frame=carry_frame, live_frame=live_frame, insts=insts, quotes=quotes, bars=bars,
                   ai=ai, today_rates=rates_summary(now.date()) if ai is not None else {},
                   short=short_rates(now.date()), ai_cache={}, trigger=trigger,
                   ai_last=ai_last_called(), ai_due=ai_due(trigger), calendar=load_calendar(now),
                   daily=daily_for_features(api, now) if ai is not None else {},  # 前回の時刻は、ai_due が書き換える前に読む
                   settings=settings, jev=jev)


SETTINGS = STATE / "fx_settings.json"
JEV_USAGE = STATE / "jev_usage.jsonl"
#: 新しい作り（ハーネス＋決まり）の決まり。変えたら application/fx_setups.py の HARNESS_VERSION を上げる
HARNESS = fx_setups.HarnessRules()


def load_settings(path: Path | None = None) -> fx_filter.FxSettings:
    """画面の設定（無い・壊れていれば既定）。"""
    try:
        raw = json.loads((path or SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return fx_filter.parse_settings(raw)


def save_settings(settings: fx_filter.FxSettings, path: Path | None = None) -> None:
    target = path or SETTINGS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def features_of(ctx: "Context", symbol: str) -> dict:
    """通貨ペアの特徴量（この回の中で使い回す）。"""
    if symbol not in ctx.features:
        bars = ctx.bars.get(symbol)
        ctx.features[symbol] = ({} if bars is None or bars.empty else
                                fx_features.market_features(bars, ctx.now, daily_closes=ctx.daily.get(symbol)))
    return ctx.features[symbol]


def consult_candidate(ctx: "Context", decision: dict, features: dict) -> tuple[dict | None, dict]:
    """決まりの候補について、設定した相手（Jev / AI）に「見送るか」と「決済の手法」を聞く（同じ相手なら1回で）。

    返すのは (見送りの答え, 決済の手法の選び)。見送りを聞かない設定なら最初は None。
    """
    settings = ctx.settings
    want_skip = settings.filter.mode != "off"
    exit_choice: dict = {"by": "fixed", "method": settings.exits.method}
    ask = {p for p, wanted in ((settings.filter.provider, want_skip), (settings.exits.selector, True))
           if wanted and p in ("jev", "gemini", "ai")}
    if not ask:
        return None, exit_choice
    symbol = decision["symbol"]
    candidate = {k: decision.get(k) for k in ("action", "symbol", "stop_pips", "setup", "invalidation_price", "hold_minutes")}
    candidate["entry_price"] = features.get("price")
    state = fx_filter.review_state(candidate, features, fx_calendar.upcoming(ctx.calendar, ctx.now, [symbol]),
                                   active_notes(ctx.now))
    verdict: dict | None = None
    if "jev" in ask:
        skip, pick = want_skip and settings.filter.provider == "jev", settings.exits.selector == "jev"
        try:
            if ctx.jev is None:
                raise RuntimeError("Jev のキーが置いていない（Secret Manager の typesafe-api-key）")
            response = ctx.jev.ask(state, fx_filter.jev_questions(skip=skip, exit_method=pick))
            usage = response.get("usage") or {}
            typesafe.log_usage(JEV_USAGE, {"at": ctx.now.isoformat(), "model": response.get("model"), **usage,
                                           "cost_usd": fx_filter.jev_cost_usd(usage)})
            if skip:
                verdict = fx_filter.jev_verdict(response, settings.filter)
            if pick:
                exit_choice = fx_filter.jev_exit_choice(response)
        except Exception as exc:  # noqa: BLE001  聞けなくても決まりのとおり進める
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if skip:
                verdict = {"provider": "jev", "error": error}
            if pick:
                exit_choice = {"by": "jev", "error": error}
    if ask & {"gemini", "ai"}:
        skip, pick = want_skip and settings.filter.provider == "gemini", settings.exits.selector == "ai"
        try:
            if ctx.ai is None:
                raise RuntimeError("AI を使わない設定")
            reply = ctx.ai.fx_review({**state, "exit_methods": fx_exits.METHODS}, ctx.now.date())
            data = reply.data or {}
            if skip:
                verdict = {**fx_filter.gemini_verdict(data), "model": reply.model}
            if pick:
                exit_choice = fx_filter.gemini_exit_choice(data)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if skip:
                verdict = {"provider": "gemini", "error": error}
            if pick:
                exit_choice = {"by": "ai", "error": error}
    if "method" not in exit_choice:  # 選べなかったら決まった手法
        exit_choice = {**exit_choice, "method": settings.exits.method}
    return verdict, exit_choice


def harness_decision(account: PaperAccount, ctx: "Context", events: list[dict]) -> tuple[dict, str, dict]:
    """新しい作り: ハーネスの数字と決まりで判断する。持っていれば決済だけ、無ければ建てる候補を探す。"""
    record = fx_setups.rules_record(HARNESS)
    p = account.position
    if p is not None:
        side = 1 if p.units > 0 else -1
        quote = ctx.quotes.get(p.symbol)
        bars = ctx.bars.get(p.symbol)
        if quote is None or not p.price or bars is None:
            return {"action": "hold"}, "", {"logic": record, "note": f"{p.symbol} の値段が取れないので決まりを確かめられない"}
        try:
            opened = dt.datetime.fromisoformat(str(p.opened_at))
        except ValueError:
            opened = ctx.now
        method = (plan_when_opened("harness", p, events, ctx.now) or {}).get("exit_method") or HARNESS.exit_method
        stop_pips = abs(p.price - p.stop) / 0.01 if p.stop else HARNESS.min_stop_pips
        result = fx_setups.exit_check(
            side=side, entry_price=p.price, stop_pips=stop_pips, price=(quote["bid"] + quote["ask"]) / 2,
            bars=bars, opened_at=opened, now=ctx.now, f=features_of(ctx, p.symbol), method=method, rules=HARNESS)
        logic = {**record, "symbol": p.symbol, "exit_method": method, "exit_level": result.exit_level,
                 "peak_pips": round(result.peak_pips, 1), "unrealized_pips": round(result.unrealized_pips, 1),
                 "evidence": result.evidence}
        if result.close:
            return {"action": "close", "reasoning": result.reason}, "決まり: " + result.reason, {"logic": logic}
        return {"action": "hold"}, "", {"logic": logic}
    symbols = [s for s in ctx.live_frame.symbols if s in ctx.bars]
    features = {s: features_of(ctx, s) for s in symbols}
    quiet = {s: fx_calendar.quiet_reason(ctx.calendar, s, ctx.now) for s in symbols}
    blocked = fx_setups.reentry_blocks(fx_setups.closed_trades(events, "harness"), ctx.now, HARNESS)
    candidate, seen = fx_setups.best_entry(features, quiet=quiet, blocked=blocked, rules=HARNESS)
    logic = {**record, "evidence": seen}
    if candidate is None:
        return {"action": "hold"}, "", {"logic": logic}
    verdict, exit_choice = consult_candidate(ctx, candidate.decision(HARNESS), features[candidate.symbol])
    decision = candidate.decision(HARNESS, exit_choice["method"])
    logic["exit_choice"] = exit_choice
    ok, note = fx_filter.apply(verdict, ctx.settings.filter)
    if verdict is not None:
        logic["filter"] = {k: v for k, v in verdict.items() if k != "usage"}
    if not ok:
        return {"action": "hold"}, "", {"logic": logic, "note": note}
    return decision, "決まり: " + decision["reasoning"], {"logic": logic, "note": note}


CALENDAR = STATE / "fx_calendar.json"
_DAILY: dict[tuple[str, dt.date], list[float]] = {}


def daily_for_features(api: GmoFxClient, now: dt.datetime) -> dict[str, list[float]]:
    """通貨ペアごとの日足の終値（直近60日）。1日1回だけ取る（見張り役は動き続けるので、覚えておく）。"""
    out = {}
    for sym in SYMBOLS:
        key = (sym, now.date())
        if key not in _DAILY:
            try:
                _DAILY[key] = daily_closes(api, sym, now)[-60:]
            except Exception as exc:  # noqa: BLE001  日足が無くても、特徴量の残りで判断する
                print(f"日足を取れなかった（{sym}）: {type(exc).__name__}: {exc}", flush=True)
                continue
        out[sym] = _DAILY[key]
    return out
#: 利用者が画面から残したメモ（AI の判断に、期限まで渡す）。{"kind": "note", id, at, text, until} と {"kind": "delete", id}
NOTES = STATE / "fx_notes.jsonl"


def active_notes(now: dt.datetime, path: Path | None = None) -> list[dict]:
    """期限内で、消されていない利用者のメモ（古い順）。"""
    try:
        rows = [json.loads(line) for line in (path or NOTES).read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
    deleted = {r.get("id") for r in rows if r.get("kind") == "delete"}
    return [r for r in rows if r.get("kind") == "note" and r.get("id") not in deleted
            and dt.datetime.fromisoformat(r["until"]) > now]


def load_calendar(now: dt.datetime) -> list:
    """経済指標の予定（1時間ごとに取り直す）。取れなくても空で進める。"""
    try:
        return fx_calendar.parse(econ_calendar.load(CALENDAR, now).get("events") or [])
    except Exception as exc:  # noqa: BLE001  予定が無くても判断は止めない
        print(f"経済指標の予定を読めなかった: {type(exc).__name__}: {exc}", flush=True)
        return []


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


#: 見張り役の生きている印。実際の口座に届いた回（last_ok_cycle）がこれより新しければ、ジョブは実際の売買をしない
WATCH_STATE = STATE / "fx_watch.json"
WATCH_FRESH_SECONDS = 5 * 60


def watcher_trades() -> bool:
    """見張り役が実際の売買を担当していて、実際の口座に届いているか（IP の制限で弾かれていたらジョブが代わる）。

    起動したばかり（WATCH_FRESH_SECONDS 以内）の見張り役も、最初の回を終えるまでは担当とみなす
    （その隙間にジョブも売買して、同じ口座で二重に建てないように）。
    """
    try:
        state = json.loads(WATCH_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not state.get("trading"):
        return False
    now = dt.datetime.now(JST)
    for key in ("last_ok_cycle", "started_at"):
        try:
            if (now - dt.datetime.fromisoformat(state.get(key) or "")).total_seconds() < WATCH_FRESH_SECONDS:
                return True
        except ValueError:
            continue
    return False


def ai_last_called() -> dt.datetime | None:
    """最後に AI を呼んだ時刻（無ければ None）。"""
    try:
        return dt.datetime.fromisoformat(AI_LAST.read_text(encoding="utf-8").strip()) if AI_LAST.exists() else None
    except (OSError, ValueError):
        return None


def ai_skip_reason(ctx: "Context") -> str:
    """AI を呼ばなかった理由を、人が読める文で。"""
    if ctx.ai_last is None:
        return "AI を呼ばなかった（前回の時刻がわからない）"
    minutes = (ctx.now - ctx.ai_last).total_seconds() / 60
    next_at = ctx.ai_last + dt.timedelta(seconds=AI_INTERVAL_SECONDS)
    return (f"前回 AI を呼んだのは {ctx.ai_last:%H:%M}（{minutes:.0f}分前）で、まだ15分たっていない。"
            f"この回は5分ごとの定期の回で、見張り役が急変などで起こした回でもないので、AI は呼ばずに今の建玉のままにした。"
            f"次に AI を呼ぶのは {next_at:%H:%M} ごろか、急変・損切り/利確の近く・時間帯の切り替わりで起こされたとき")


def ai_due(trigger: str) -> bool:
    """この回で AI を呼ぶか。見張り役が起こした回は必ず。そうでなければ前回から AI_INTERVAL_SECONDS たったら。"""
    now = dt.datetime.now(JST)
    try:
        last = dt.datetime.fromisoformat(AI_LAST.read_text(encoding="utf-8").strip()) if AI_LAST.exists() else None
    except ValueError:
        last = None
    # 決まりが決済を持つ建玉（harness）だけの理由で起こされた回では、AI は呼ばない（費用と、AI の判断を混ぜないため）
    for_ai = bool(trigger) and "harness の建玉" not in trigger
    due = for_ai or last is None or (now - last).total_seconds() >= AI_INTERVAL_SECONDS - 60
    if due:
        try:
            AI_LAST.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
        except OSError:
            pass
    return due


def live_watch_rows(events: list[dict]) -> list[dict]:
    """見張り役に渡す実際の建玉（建値・向き・損切り・利確と、建てたときの予想）。"""
    now = dt.datetime.now(JST)
    rows = []
    for p in book_from_events(events).values():
        plan = dict(plan_when_opened(p.account, p, events, now) or {})
        side = 1 if p.side == "BUY" else -1
        if p.price is not None:  # 向きと合わない予想は使わない（買いなら、崩れる値段は建値より下・目標は上）
            if plan.get("invalidation_price") and side * (p.price - plan["invalidation_price"]) <= 0:
                plan["invalidation_price"] = None
            if plan.get("target_price") and side * (plan["target_price"] - p.price) <= 0:
                plan["target_price"] = None
        row = {"venue": "live", "account": p.account, "symbol": p.symbol, "stop": p.stop, "take": p.take,
               "price": p.price, "side": p.side, "opened_at": p.opened_at,
               "thesis": {k: plan.get(k) for k in ("invalidation_price", "target_price", "hold_minutes", "setup")}}
        if p.account == "harness":  # 決済は決まりが持つ。見張り役は、決まりが最後に出した「利益を守る線」を見る
            row["managed"] = "rules"
            row["exit_level"] = next((e["logic"].get("exit_level") for e in reversed(events)
                                      if e.get("kind") == "live_decision" and e.get("account") == p.account
                                      and isinstance(e.get("logic"), dict) and e.get("at", "") >= str(p.opened_at)),
                                     None)
        rows.append(row)
    return rows


def write_positions(accounts: dict[str, PaperAccount]) -> None:
    """見張り役のための、今の建玉の要約（ペーパーと実際）。"""
    rows = [{"venue": "paper", "account": a.name, "symbol": a.position.symbol, "stop": a.position.stop,
             "take": a.position.take} for a in accounts.values() if a.position is not None]
    rows += live_watch_rows(fx_live_log.load())
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


def trade_live(ctx: "Context") -> bool:
    """実際の口座で、判断する者ごとに、実際の建玉を見て判断し、発注する。口座に届かなかったら False。"""
    session = LiveSession.open()
    if session is None:
        return False
    intents: list[Intent] = []
    for name in session.config.accounts:
        view = live_view(name, session)
        switched_on = ctx.settings.enabled(name)
        if not switched_on and view.position is None:
            continue  # 画面で止めている（建玉があれば、決済の判断だけは続ける）
        account_frame = ctx.live_frame
        for decision, reason, extra in decide(name, view, ctx, session.log + session.events, live=True):
            equity = session.equity
            approved, rejected = check(
                decision, account_frame, equity=equity, holding=view.position.symbol if view.position else None,
                spread_pips={s: q["spread"] for s, q in ctx.quotes.items()},
                prices={s: q["ask"] for s, q in ctx.quotes.items()}, instruments=ctx.insts, halted=False,
            )
            if approved is not None and approved.action != "close" and not switched_on:
                approved, rejected = None, "画面で止めている（新しく建てない。建玉の決済は続ける）"
            session.events.append({"kind": "live_decision", "at": ctx.now.isoformat(), "account": name,
                                   "decision": {k: v for k, v in decision.items() if k != "context"},
                                   "approved": approved is not None, "rejected": rejected,
                                   **{k: v for k, v in extra.items() if k in ("logic", "model", "prompt_version",
                                                                             "confidence", "reused", "error", "note",
                                                                             "trigger")}})
            if approved is None:
                continue
            intents.append(Intent(account=name, action="close" if approved.action == "close" else "open",
                                  at=ctx.now.isoformat(), symbol=approved.symbol, units=approved.units,
                                  stop_pips=approved.stop_pips, take_pips=approved.take_pips, reason=reason))
    session.execute(intents)
    session.finish()
    return True


def live_view(name: str, session) -> PaperAccount:
    """実際の口座を、判断する者が見る形にする（建玉は、このシステムがその判断する者のために建てたもの）。"""
    p = session.position_of(name)
    position = None
    if p is not None:
        sign = 1 if p.side == "BUY" else -1
        position = PaperPosition(p.symbol, sign * p.size, float(p.price or 0), p.stop, p.take, p.opened_at, "実際の建玉")
    equity = session.equity
    return PaperAccount(name, equity, equity, position, equity=equity, day_start_equity=equity)

