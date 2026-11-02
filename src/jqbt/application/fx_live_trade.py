"""FX の自動売買（実際のお金）の計画と安全装置。I/O なし。

判断する者（AI・金利差＋流れ など）は、**実際の口座の状態** を見て判断する。ペーパー運用（シミュレーション）とは独立で、
ペーパー運用を止めても実際の売買は続く。ここでは判断（``Intent``）から何を発注するかを決め、
発注そのものは ``scripts/fx_live_trade.py`` が ``GmoFxTrader`` で行う。

  * 新規（open）→ 成行で建て、すぐに GMOコイン側に損切り（と利確）を置く（システムが止まっても効く）
  * 判断で閉じる（close）→ 置いてある損切りを取り消し、成行で決済する
  * 損切り・利確に掛かったら、GMOコイン側の注文で決済される（次の回で、無くなった建玉として記録する）

## 方針（利用者が決めた）

**止めるのは、致命的な損失（入金額を超える損＝不足金）のおそれがあるときだけ。** それ以外では止めない。
持ち続ける理由が無くなった建玉は閉じる（ルールの条件が崩れた、AI が閉じると判断した、損切りの値段に届いた）。

## 安全装置（どれか1つでも当てはまれば、新しく建てない。決済はいつでも通す）

  * 有効にしていない（``enabled``）、キルスイッチが入っている
  * 1口座の数量は ``max_units`` まで。まねる口座で、すでに建玉を持っている
  * 取引余力が、必要な証拠金の ``margin_buffer`` 倍に足りない分は数量を減らす。最小の注文数量に届かなければ建てない
  * ``daily_loss_limit`` を決めたときだけ、その日の初めから評価額がその割合だけ減ったら建てない（既定は使わない）

## 致命的な損失を防ぐ（``is_fatal``）

証拠金維持率が ``fatal_margin_ratio``（%）を下回ったら、全部の建玉を成行で決済し、キルスイッチを入れる。
GMOコインのロスカットは100%だが、急変ではロスカットの値段を飛び越えて入金額を超える損が出うるので、その手前で降りる。
ふだんの損は、損切り（1回で資金の最大5%）で先に止まる。
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
    #: 1口座あたりの数量の上限（通貨）。判断する者が状況に応じて決めた数量を、ここで切る（利用者が決めた: 1,000）
    max_units: int = 1_000
    #: 最小の注文数量（GMOコインは各通貨ペア100通貨）
    min_units: int = 100
    #: その日の初めから評価額がこの割合だけ減ったら、その日は新しく建てない。None なら使わない（既定。
    #: 利用者の方針: 致命的でない損失では止めない）
    daily_loss_limit: float | None = None
    #: 証拠金維持率（%）がこれを下回ったら、全部決済してキルスイッチ（致命的な損失＝不足金を防ぐ）
    #: 1,000通貨（約15倍）では、維持率150%は損切りより手前に来てしまうので、ロスカット（100%）の少し手前にする
    fatal_margin_ratio: float = 110.0
    #: 取引余力が、必要な証拠金（新規は20倍）のこの倍数に足りない分は数量を減らす
    margin_buffer: float = 1.2
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
    price: float | None = None
    stop: float | None = None
    take: float | None = None
    opened_at: str = ""


@dataclass(frozen=True)
class Intent:
    """判断する者が、実際の口座で何をしたいか（枠は通したあと）。"""

    account: str
    action: str  # open / close
    at: str
    symbol: str = ""
    units: float = 0.0  # 符号つき（正は買い）
    stop_pips: float | None = None
    take_pips: float | None = None
    reason: str = ""


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
                                              int(e["position_id"]), list(e.get("protect_root_ids") or []),
                                              price=e.get("price"), opened_at=e.get("at", ""))
        elif kind == "live_protect" and e.get("account") in book:
            position = book[e["account"]]
            if e.get("root_order_id") is not None:
                position.protect_root_ids.append(int(e["root_order_id"]))
            position.stop, position.take = e.get("stop"), e.get("take")
        elif kind in ("live_close", "live_gone") and e.get("account") in book:
            if book[e["account"]].position_id == int(e.get("position_id", -1)):
                del book[e["account"]]
    return book


def gone_positions(book: dict[str, LivePosition], open_position_ids: set[int]) -> list[LivePosition]:
    """記録上は持っているのに、GMOコインにもう無い建玉（損切り・利確・ロスカットで決済された）。"""
    return [p for p in book.values() if p.position_id not in open_position_ids]


def plan(
    intents: list[Intent],
    book: dict[str, LivePosition],
    config: LiveConfig,
    *,
    killed: bool,
    equity: float,
    day_start_equity: float | None,
    available: float,
    prices: dict[str, float],
) -> list[Open | Close | Skip]:
    """判断（枠を通したもの）から、実際の口座で何をするかを決める。決済はいつでも通す。"""
    actions: list[Open | Close | Skip] = []
    for intent in intents:
        account = intent.account
        if account not in config.accounts:
            continue
        if intent.action == "close":
            if account in book:
                p = book[account]
                actions.append(Close(account, p.symbol, p.side, p.position_id, p.size, tuple(p.protect_root_ids),
                                     client_id(account, intent.at, "c")))
            continue
        if intent.action != "open":
            continue
        size = min(int(abs(intent.units)), config.max_units, _affordable(intent.symbol, available, config, prices))
        reason = _blocked(account, intent.symbol, size, book, config, killed, equity, day_start_equity, available, prices)
        if not reason and not intent.stop_pips:
            reason = "損切りの無い判断は、実際の口座では建てない"
        if reason:
            actions.append(Skip(account, reason))
            continue
        actions.append(Open(account, intent.symbol, "BUY" if intent.units > 0 else "SELL", size,
                            round(float(intent.stop_pips), 1),
                            round(float(intent.take_pips), 1) if intent.take_pips else None,
                            client_id(account, intent.at, "o")))
    return actions


def _blocked(account, symbol, size, book, config, killed, equity, day_start_equity, available, prices) -> str:
    if not config.enabled:
        return "自動売買が有効になっていない（fx_live_enabled）"
    if killed:
        return "キルスイッチが入っている"
    if account in book:
        return "この口座は、実際の建玉をもう持っている"
    if config.daily_loss_limit and day_start_equity and equity <= day_start_equity * (1 - config.daily_loss_limit):
        return f"今日の損失が上限（{config.daily_loss_limit:.0%}）に届いた"
    if size < config.min_units:
        return f"取引余力の範囲の数量（{size}通貨）が最小の注文数量（{config.min_units}通貨）に届かない（余力 {available:,.0f}円）"
    return ""


def _affordable(symbol: str, available: float, config: LiveConfig, prices: dict[str, float]) -> int:
    """取引余力で建てられる数量（必要な証拠金の margin_buffer 倍まで）。"""
    price = prices.get(symbol) or 0.0
    if price <= 0:
        return 0
    return max(0, int(available / (price / config.leverage_for_entry * config.margin_buffer)))


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
