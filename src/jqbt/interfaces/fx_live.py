"""FX の実際の口座（GMOコイン）の部品: 発注のセッションと、約定・建玉・資産の照会。

  * ``LiveSession``: 1回分の実際の口座。判断する者が見る状態を用意し、判断（Intent）を発注にする。
    建てたらすぐ GMOコイン側に損切り（と利確）を置き、置けなければ決済してキルスイッチを入れる
  * ``sync``: 約定・建玉・資産を読んで記録する（読むだけ）

計画と安全装置は ``application.fx_live_trade``（I/O なし）、発注のクライアントは ``infrastructure.gmo_fx_trader``。
ここは、その2つをつなぐ段取りだけを持つ。ジョブ（scripts/fx_paper.py）と見張り役（scripts/fx_watch.py）の両方から使う。
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import httpx
import pandas as pd

from ..application.fx_live import new_executions, slippage_pips
from ..application.fx_live_trade import (
    Close,
    Intent,
    LiveConfig,
    Open,
    Skip,
    book_from_events,
    gone_positions,
    is_fatal,
    plan,
    stop_take_prices,
    today_start_equity,
)
from ..infrastructure import fx_live_log
from ..infrastructure.env import load_env
from ..infrastructure.gmo_fx import GmoError, GmoFxClient
from ..infrastructure.gmo_fx_store import fetch_day
from ..infrastructure.gmo_fx_trader import GmoFxTrader
from ..infrastructure.secrets import SecretError, build_secrets

JST = dt.timezone(dt.timedelta(hours=9))
STATE = Path(os.environ.get("JQBT_STATE_DIR") or Path(__file__).resolve().parents[3] / "state")
KILL = STATE / "KILL_fx_live"


def config_from_env() -> LiveConfig:
    return LiveConfig(enabled=os.environ.get("FX_LIVE_ENABLED", "").lower() == "true")


def trader_from_secrets() -> GmoFxTrader | None:
    try:
        secrets = build_secrets()
        key, secret = secrets.get("gmo_fx_trade_api_key"), secrets.get("gmo_fx_trade_api_secret")
    except SecretError:
        return None
    return GmoFxTrader(key, secret) if key and secret else None


def kill(reason: str) -> None:
    KILL.parent.mkdir(parents=True, exist_ok=True)
    KILL.write_text(f"{dt.datetime.now(JST).isoformat(timespec='seconds')} {reason}", encoding="utf-8")


def fill_of(trader: GmoFxTrader, order_id: int, tries: int = 6) -> dict | None:
    """成行の約定を待って、建玉IDと約定の値段を返す。"""
    for _ in range(tries):
        rows = trader.executions_of(order_id)
        if rows:
            return rows[0]
        time.sleep(1)
    return None


def execute(trader: GmoFxTrader, action, now: str) -> list[dict]:
    events: list[dict] = []
    if isinstance(action, Skip):
        return [{"kind": "live_skip", "at": now, "account": action.account, "reason": action.reason}]
    if isinstance(action, Close):
        if action.protect_root_ids:
            try:
                trader.cancel(list(action.protect_root_ids))
            except GmoError as exc:  # もう約定・取消済みのこともある
                events.append({"kind": "live_note", "at": now, "account": action.account,
                               "note": f"損切り注文の取消: {exc}"})
        result = trader.market_close(action.symbol, action.position_side, action.position_id, action.size,
                                     action.client_order_id)
        events.append({"kind": "live_close", "at": now, "account": action.account, "symbol": action.symbol,
                       "position_id": action.position_id, "order_id": result.get("orderId"),
                       "client_order_id": action.client_order_id})
        return events

    # 新規
    result = trader.market_open(action.symbol, action.side, action.size, action.client_order_id)
    order_id = result.get("orderId")
    fill = fill_of(trader, order_id) if order_id else None
    if not fill:
        kill(f"{action.account} の新規注文（{order_id}）の約定が確かめられない")
        return [{"kind": "live_error", "at": now, "account": action.account,
                 "error": "約定を確かめられないので、キルスイッチを入れた。アプリで建玉を確かめること", "order_id": order_id}]
    position_id, price = int(fill["positionId"]), float(fill["price"])
    events.append({"kind": "live_open", "at": now, "account": action.account, "symbol": action.symbol,
                   "side": action.side, "size": action.size, "position_id": position_id, "price": price,
                   "order_id": order_id, "client_order_id": action.client_order_id})
    stop, take = stop_take_prices(action.side, price, action.stop_pips, action.take_pips)
    for attempt in range(3):
        try:
            protect = trader.protect(action.symbol, action.side, position_id, action.size, stop, take,
                                     action.client_order_id.replace("o", "p", 1))
            events.append({"kind": "live_protect", "at": now, "account": action.account, "position_id": position_id,
                           "root_order_id": protect.get("rootOrderId"), "stop": stop, "take": take})
            return events
        except GmoError as exc:
            last = str(exc)
            time.sleep(1.5)
    # 損切りを置けない建玉は持たない
    trader.market_close(action.symbol, action.side, position_id, action.size, action.client_order_id.replace("o", "f", 1))
    kill(f"{action.account} の損切りを置けなかった（{last}）")
    events.append({"kind": "live_close", "at": now, "account": action.account, "symbol": action.symbol,
                   "position_id": position_id, "reason": "損切りを置けなかったので決済（安全装置）"})
    events.append({"kind": "live_error", "at": now, "account": action.account,
                   "error": f"損切りを置けなかった（{last}）。決済してキルスイッチを入れた"})
    return events


class LiveSession:
    """1回分の実際の口座。判断する者が見る状態（建玉・評価額）を用意し、判断を発注にする。

    ペーパー運用とは独立。ペーパー運用を止めていても動く。
    """

    def __init__(self, trader: GmoFxTrader, config: LiveConfig, log: list[dict], now: str) -> None:
        self.trader, self.config, self.log, self.now = trader, config, log, now
        self.book = book_from_events(log)
        self.events: list[dict] = []
        self.assets: dict = {}
        self.prices: dict[str, float] = {}

    @classmethod
    def open(cls) -> "LiveSession | None":
        """口座を読み、無くなった建玉を記録し、致命的な損失のおそれがあれば全部決済する。使えなければ None。"""
        config = config_from_env()
        now = dt.datetime.now(JST).isoformat(timespec="seconds")
        trader = trader_from_secrets()
        if trader is None:
            return None
        session = cls(trader, config, fx_live_log.load(), now)
        try:
            positions = trader.open_positions()
            open_ids = {int(p["positionId"]) for p in positions}
            last_ready = next((e for e in reversed(session.log) if e.get("kind") == "live_ready"), None)
            if not last_ready or not last_ready.get("ok"):
                session.events.append({"kind": "live_ready", "at": now, "ok": True, "enabled": config.enabled,
                                       "note": "発注用のキーで照会できた（IP の制限を通った）"})
            for p in gone_positions(session.book, open_ids):
                session.events.append({"kind": "live_gone", "at": now, "account": p.account,
                                       "position_id": p.position_id,
                                       "note": "GMOコイン側で決済された（損切り・利確・ロスカット）"})
                del session.book[p.account]
            assets = trader.assets()
            session.assets = assets[0] if isinstance(assets, list) and assets else (assets or {})
            session.prices = {t["symbol"]: float(t["ask"]) for t in trader.ticker()}
            fatal = is_fatal(session.assets, bool(open_ids), config)
            if fatal:
                kill(f"致命的な損失のおそれ: {fatal}")
                session.events.append({"kind": "live_fatal", "at": now, "reason": fatal})
                for position in positions:
                    trader.market_close(position["symbol"], position["side"], int(position["positionId"]),
                                        int(float(position["size"])), f"x{position['positionId']}"[:36])
                    session.events.append({"kind": "live_close", "at": now, "symbol": position["symbol"],
                                           "position_id": int(position["positionId"]),
                                           "reason": f"致命的な損失を防ぐ: {fatal}"})
                session.finish()
                return None
        except GmoError as exc:
            session.events.append({"kind": "live_error", "at": now, "error": str(exc)[:300]})
            last_ready = next((e for e in reversed(session.log) if e.get("kind") == "live_ready"), None)
            if not last_ready or last_ready.get("ok"):
                session.events.append({"kind": "live_ready", "at": now, "ok": False, "note": str(exc)[:200]})
            session.finish()
            return None
        return session

    @property
    def equity(self) -> float:
        return float(self.assets.get("equity") or 0)

    def position_of(self, account: str):
        return self.book.get(account)

    def execute(self, intents: list[Intent]) -> None:
        snapshots = [e for e in self.log if e.get("kind") == "snapshot"]
        actions = plan(intents, self.book, self.config, killed=KILL.exists(), equity=self.equity,
                       day_start_equity=today_start_equity(snapshots, dt.date.today()),
                       available=float(self.assets.get("availableAmount") or 0), prices=self.prices)
        for action in actions:
            try:
                self.events += execute(self.trader, action, self.now)
            except GmoError as exc:
                self.events.append({"kind": "live_error", "at": self.now, "account": getattr(action, "account", ""),
                                    "error": str(exc)[:300]})

    def finish(self, quiet: bool = False) -> None:
        fx_live_log.append(self.events)
        if not quiet:
            for e in self.events:
                print(f"  [実際] {e['kind']} {e.get('account', '')} {e.get('symbol', '')} "
                      f"{e.get('reason', '') or e.get('error', '') or e.get('note', '')}")


# ----------------------------------------------------------------------
# 照会（読むだけ）
# ----------------------------------------------------------------------
SYMBOLS = ("USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY")


def quote_at(api: GmoFxClient, symbol: str, at: dt.datetime, cache: dict) -> tuple[float, float] | None:
    """約定した1分足の始まりの売値・買値。GMO の営業日（朝6時または7時に始まる）で足を取る。"""
    minute = pd.Timestamp(at.astimezone(JST).replace(tzinfo=None)).floor("min")
    for day in (minute.date(), (minute - pd.Timedelta(days=1)).date()):
        key = (symbol, day)
        if key not in cache:
            try:
                cache[key] = fetch_day(api, symbol, day)
            except (GmoError, httpx.HTTPError):  # まだ始まっていない日（深夜0時〜朝）など。ずれは出さずに進む
                cache[key] = pd.DataFrame()
        bars = cache[key]
        if not bars.empty and minute in bars.index:
            row = bars.loc[minute]
            return float(row["bid_open"]), float(row["ask_open"])
    return None


def sync(path: Path | None = None, quiet: bool = False) -> dict:
    """1回読む。返すのは、新しく記録した約定の数と、読めなかったもの。"""
    load_env()
    path = path or fx_live_log.DEFAULT_PATH
    try:
        secrets = build_secrets()
        key, secret = secrets.get("gmo_fx_api_key"), secrets.get("gmo_fx_api_secret")
    except SecretError as exc:
        return {"new": 0, "errors": [f"キーを読めません: {exc}"]}
    if not (key and secret):
        return {"new": 0, "errors": ["GMOコインのキーがありません"]}
    api = GmoFxClient(key, secret)
    now = dt.datetime.now(JST).isoformat(timespec="seconds")
    events, errors, cache = [], [], {}
    known = fx_live_log.known_execution_ids(fx_live_log.load(path))

    for symbol in SYMBOLS:
        try:
            fetched = api.latest_executions(symbol)
        except GmoError as exc:
            errors.append(f"約定（{symbol}）: {exc}")
            break  # 権限が無いなら、どの通貨ペアでも同じ
        for e in new_executions(fetched, known):
            at = dt.datetime.fromisoformat(str(e["timestamp"]).replace("Z", "+00:00"))
            quote = quote_at(api, symbol, at, cache)
            slip = slippage_pips(e["side"], float(e["price"]), *quote) if quote else None
            events.append({"kind": "execution", "at": at.astimezone(JST).isoformat(timespec="seconds"),
                           **e, "bid_at": quote[0] if quote else None, "ask_at": quote[1] if quote else None,
                           "slippage_pips": slip})
    snapshot = {"kind": "snapshot", "at": now}
    for name, call in (("assets", api.assets), ("positions", api.open_positions), ("summary", api.position_summary)):
        try:
            snapshot[name] = call()
        except GmoError as exc:
            errors.append(f"{name}: {exc}")
    events.append(snapshot)
    if errors:
        events.append({"kind": "error", "at": now, "errors": errors})
    fx_live_log.append(events, path)
    result = {"new": sum(e["kind"] == "execution" for e in events), "errors": errors}
    if not quiet:
        print(f"実際の約定: 新しく {result['new']} 件" + (f" / 読めなかったもの: {'; '.join(errors)}" if errors else ""))
    return result


