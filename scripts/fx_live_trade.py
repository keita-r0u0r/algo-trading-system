"""FX の自動売買（**実際のお金**）。判断する者（AI・金利差＋流れ）が、実際の口座の状態を見て判断し、ここで発注する。

ペーパー運用（シミュレーション）とは独立。15分ごとの回（scripts/fx_paper.py）の中で、ペーパー運用を止めていても動く。

  * ``FX_LIVE_ENABLED=true`` のときだけ注文を出す（infra の ``fx_live_enabled``）。それ以外は、何をしたかを記録するだけ
  * キルスイッチ ``state/KILL_fx_live`` が在れば、新しく建てない（画面の「実際の約定」から入れられる）
  * 発注用のキー（gmo-fx-trade-api-key / secret）でだけ動く
  * 建てたら、すぐに GMOコイン側に損切り（と利確）を置く。置けなかったら、その建玉を成行で決済し、キルスイッチを入れる
  * 何をしたか・しなかったかは、すべて state/fx_live.jsonl に記録する
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.application.fx_live_trade import (  # noqa: E402
    Close,
    Intent,
    LiveConfig,
    Open,
    Skip,
    book_from_events,
    is_fatal,
    gone_positions,
    plan,
    stop_take_prices,
    today_start_equity,
)
from jqbt.infrastructure import fx_live_log  # noqa: E402
from jqbt.infrastructure.gmo_fx import GmoError  # noqa: E402
from jqbt.infrastructure.gmo_fx_trader import GmoFxTrader  # noqa: E402
from jqbt.infrastructure.secrets import SecretError, build_secrets  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
STATE = Path(__file__).resolve().parents[1] / "state"
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
