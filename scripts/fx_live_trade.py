"""FX の自動売買（**実際のお金**）。ペーパー運用の口座の判断を、実際の口座で小さくまねる。

ペーパー運用（scripts/fx_paper.py）の1回ごとに呼ばれる。単独では動かさない。

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
    LiveConfig,
    Open,
    Skip,
    book_from_events,
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


def run(paper_events: list[dict], quiet: bool = False) -> dict:
    """この回のペーパー運用の出来事を受け取り、実際の口座で必要な注文を出す。"""
    config = config_from_env()
    now = dt.datetime.now(JST).isoformat(timespec="seconds")
    relevant = [e for e in paper_events if e.get("account") in config.accounts and e.get("kind") in ("open", "close")]
    trader = trader_from_secrets()
    if trader is None:
        if relevant and config.enabled:
            fx_live_log.append([{"kind": "live_error", "at": now, "error": "発注用のキーが無い"}])
        return {"orders": 0, "note": "発注用のキーが無い"}

    log = fx_live_log.load()
    book = book_from_events(log)
    events: list[dict] = []
    try:
        open_ids = {int(p["positionId"]) for p in trader.open_positions()}
        for p in gone_positions(book, open_ids):
            events.append({"kind": "live_gone", "at": now, "account": p.account, "position_id": p.position_id,
                           "note": "GMOコイン側で決済された（損切り・利確・ロスカット）"})
            del book[p.account]
        if not relevant:
            fx_live_log.append(events)
            return {"orders": 0}
        assets = trader.assets()
        assets = assets[0] if isinstance(assets, list) and assets else assets
        prices = {t["symbol"]: float(t["ask"]) for t in trader.ticker()}
        snapshots = [e for e in log if e.get("kind") == "snapshot"]
        actions = plan(relevant, book, config, killed=KILL.exists(), equity=float(assets.get("equity") or 0),
                       day_start_equity=today_start_equity(snapshots, dt.date.today()),
                       available=float(assets.get("availableAmount") or 0), prices=prices)
        for action in actions:
            events += execute(trader, action, now)
    except GmoError as exc:
        events.append({"kind": "live_error", "at": now, "error": str(exc)[:300]})
    fx_live_log.append(events)
    orders = sum(e["kind"] in ("live_open", "live_close") for e in events)
    if not quiet and events:
        for e in events:
            print(f"  [実際] {e['kind']} {e.get('account', '')} {e.get('symbol', '')} {e.get('reason', '') or e.get('error', '')}")
    return {"orders": orders}
