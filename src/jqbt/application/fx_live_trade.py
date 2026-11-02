"""FX の自動売買（実際のお金）の計画と安全装置。I/O なし。

ペーパー運用の口座の判断を、実際の口座で **小さく** まねる。何を発注するかをここで決め、
発注そのものは ``scripts/fx_live_trade.py`` が ``GmoFxTrader`` で行う。

## まねるもの

  * この回のペーパー運用で、まねる口座が建てた（open）→ 実際にも成行で建てる
  * この回のペーパー運用で、まねる口座が判断で閉じた（close）→ 実際の建玉も成行で決済する
  * 損切り・利確は、建てた直後に GMOコイン側に置く（システムが止まっても効く）。
    ペーパー運用の損切り・利確に掛かったときは、実際の口座でも GMOコイン側の注文で決済される

## 方針（利用者が決めた）

**止めるのは、致命的な損失（入金額を超える損＝不足金）のおそれがあるときだけ。** それ以外では止めない。
持ち続ける理由が無くなった建玉は閉じる（ルールの条件が崩れた、AI が閉じると判断した、損切りの値段に届いた）。

## 安全装置（どれか1つでも当てはまれば、新しく建てない。決済はいつでも通す）

  * 有効にしていない（``enabled``）、キルスイッチが入っている
  * 1口座の数量は ``max_units`` まで。まねる口座で、すでに建玉を持っている（損の大きさを入金額より十分小さく保つ）
  * 取引余力が、必要な証拠金の ``margin_buffer`` 倍に足りない
  * ``daily_loss_limit`` を決めたときだけ、その日の初めから評価額がその割合だけ減ったら建てない（既定は使わない）

## 致命的な損失を防ぐ（``is_fatal``）

証拠金維持率が ``fatal_margin_ratio``（%）を下回ったら、全部の建玉を成行で決済し、キルスイッチを入れる。
GMOコインのロスカットは100%だが、急変ではロスカットの値段を飛び越えて入金額を超える損が出うるので、その手前で降りる。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LiveConfig:
    enabled: bool = False
    #: まねる口座（ペーパー運用の口座の名前）
    accounts: tuple[str, ...] = ("ai", "carry_trend_USD_JPY")
    #: 1口座あたりの数量の上限（通貨）。ペーパー運用がこれより大きく建てても、ここで切る
    max_units: int = 100
    #: その日の初めから評価額がこの割合だけ減ったら、その日は新しく建てない。None なら使わない（既定。
    #: 利用者の方針: 致命的でない損失では止めない）
    daily_loss_limit: float | None = None
    #: 証拠金維持率（%）がこれを下回ったら、全部決済してキルスイッチ（致命的な損失＝不足金を防ぐ）
    fatal_margin_ratio: float = 150.0
    #: 取引余力が、必要な証拠金（新規は20倍）のこの倍数に足りなければ建てない
    margin_buffer: float = 1.5
    leverage_for_entry: float = 20.0


@dataclass(frozen=True)
class Open:
    account: str
    symbol: str
    side: str  # BUY / SELL
    size: int
    stop_pips: float
    take_pips: float | None
    client_order_id: str


@dataclass(frozen=True)
class Close:
    account: str
    symbol: str
    position_side: str
    position_id: int
    size: int
    protect_root_ids: tuple[int, ...]
    client_order_id: str


@dataclass(frozen=True)
class Skip:
    account: str
    reason: str


@dataclass
class LivePosition:
    account: str
    symbol: str
    side: str
    size: int
    position_id: int
    protect_root_ids: list[int] = field(default_factory=list)


def client_id(account: str, at: str, kind: str) -> str:
    """判断ごとに一意な顧客注文ID（36文字以内の英数字）。同じ判断で二度注文しないために使う。"""
    short = "".join(ch for ch in account if ch.isalnum())[:14]
    stamp = "".join(ch for ch in at if ch.isdigit())[:12]
    return f"{kind}{short}{stamp}"[:36]


def book_from_events(events: list[dict[str, Any]]) -> dict[str, LivePosition]:
    """記録から、口座ごとの実際の建玉（このシステムが建てたもの）を組み立てる。"""
    book: dict[str, LivePosition] = {}
    for e in events:
        kind = e.get("kind")
        if kind == "live_open" and e.get("position_id"):
            book[e["account"]] = LivePosition(e["account"], e["symbol"], e["side"], int(e["size"]),
                                              int(e["position_id"]), list(e.get("protect_root_ids") or []))
        elif kind == "live_protect" and e.get("account") in book:
            book[e["account"]].protect_root_ids.append(int(e["root_order_id"]))
        elif kind in ("live_close", "live_gone") and e.get("account") in book:
            if book[e["account"]].position_id == int(e.get("position_id", -1)):
                del book[e["account"]]
    return book


def gone_positions(book: dict[str, LivePosition], open_position_ids: set[int]) -> list[LivePosition]:
    """記録上は持っているのに、GMOコインにもう無い建玉（損切り・利確・ロスカットで決済された）。"""
    return [p for p in book.values() if p.position_id not in open_position_ids]


def plan(
    paper_events: list[dict[str, Any]],
    book: dict[str, LivePosition],
    config: LiveConfig,
    *,
    killed: bool,
    equity: float,
    day_start_equity: float | None,
    available: float,
    prices: dict[str, float],
) -> list[Open | Close | Skip]:
    """この回のペーパー運用の出来事から、実際の口座で何をするかを決める。"""
    actions: list[Open | Close | Skip] = []
    for e in paper_events:
        account = e.get("account")
        if account not in config.accounts:
            continue
        if e.get("kind") == "close" and account in book and "損切り" not in str(e.get("reason")) \
                and "利確" not in str(e.get("reason")):
            # 判断で閉じた（損切り・利確は GMOコイン側の注文で決済されるので、ここでは出さない）
            p = book[account]
            actions.append(Close(account, p.symbol, p.side, p.position_id, p.size, tuple(p.protect_root_ids),
                                 client_id(account, e.get("at", ""), "c")))
        elif e.get("kind") == "open":
            reason = _blocked(account, e, book, config, killed, equity, day_start_equity, available, prices)
            if reason:
                actions.append(Skip(account, reason))
                continue
            side = "BUY" if float(e["units"]) > 0 else "SELL"
            price = float(e["price"])
            stop_pips = abs(price - float(e["stop"])) / 0.01 if e.get("stop") is not None else None
            take_pips = abs(float(e["take"]) - price) / 0.01 if e.get("take") is not None else None
            if not stop_pips:
                actions.append(Skip(account, "損切りの無い判断は、実際の口座ではまねない"))
                continue
            size = min(int(abs(float(e["units"]))), config.max_units)
            actions.append(Open(account, e["symbol"], side, size, round(stop_pips, 1),
                                round(take_pips, 1) if take_pips else None, client_id(account, e.get("at", ""), "o")))
    return actions


def _blocked(account, e, book, config, killed, equity, day_start_equity, available, prices) -> str:
    if not config.enabled:
        return "自動売買が有効になっていない（fx_live_enabled）"
    if killed:
        return "キルスイッチが入っている"
    if account in book:
        return "この口座は、実際の建玉をもう持っている"
    if config.daily_loss_limit and day_start_equity and equity <= day_start_equity * (1 - config.daily_loss_limit):
        return f"今日の損失が上限（{config.daily_loss_limit:.0%}）に届いた"
    size = min(int(abs(float(e["units"]))), config.max_units)
    price = prices.get(e["symbol"]) or float(e["price"])
    needed = size * price / config.leverage_for_entry * config.margin_buffer
    if available < needed:
        return f"取引余力が足りない（{available:,.0f}円 < {needed:,.0f}円）"
    if size < 1:
        return "数量が0"
    return ""


def stop_take_prices(side: str, fill: float, stop_pips: float, take_pips: float | None, pip: float = 0.01) -> tuple[float, float | None]:
    """約定した値段から、損切り・利確の値段を出す。"""
    sign = 1 if side == "BUY" else -1
    stop = round(fill - sign * stop_pips * pip, 3)
    take = round(fill + sign * take_pips * pip, 3) if take_pips else None
    return stop, take


def today_start_equity(snapshots: list[dict[str, Any]], today: dt.date) -> float | None:
    """今日の最初の記録の評価額（1日の損失の基準）。"""
    for s in snapshots:
        if str(s.get("at", "")).startswith(today.isoformat()):
            assets = s.get("assets")
            assets = assets[0] if isinstance(assets, list) and assets else assets
            if isinstance(assets, dict) and assets.get("equity") is not None:
                return float(assets["equity"])
    return None


def is_fatal(assets: dict[str, Any], has_positions: bool, config: LiveConfig) -> str:
    """致命的な損失（不足金）のおそれ。理由を返す（無ければ空）。建玉が無いときは問わない。"""
    if not has_positions:
        return ""
    equity = float(assets.get("equity") or 0)
    if equity <= 0:
        return f"評価額が0以下（{equity:,.0f}円）"
    ratio = assets.get("marginRatio")
    if ratio not in (None, "") and float(ratio) < config.fatal_margin_ratio:
        return f"証拠金維持率が {float(ratio):.0f}%（{config.fatal_margin_ratio:.0f}% 未満）"
    return ""
