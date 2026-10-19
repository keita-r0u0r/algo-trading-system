"""運用の流れ（売買候補 → 約定の記録 → 評価 → 増額の判定）。

**注文は出さない。** 売買候補を出し、人が発注した約定を記録し、想定とのずれを測る。
ペーパー（仮想の約定）でも、同じ流れで回せる。

## 守っていること

  * 凍結: 戦略とパラメータは ``init`` で固定し、運用中は変えない。変えるなら検証のやり直し
  * 古いデータでは候補を出さない（実運用のとき）。Free プランは12週間遅れなので、これが働く
  * 候補は、リスク制限（``core/risk.py``）を通ったものだけ
  * 制限を緩める変更は、申請から24時間後に有効になる。厳しくする変更は即時
  * 約定は台帳に追記する。想定の約定価格（約定日の始値）との差を、そのたびに計測する
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from typing import Any, Callable

import pandas as pd

from ..core.accounting import apply_fill
from ..core.contracts import Fill, Order, Portfolio, Side
from ..core.gates import (
    Check,
    GateThresholds,
    LiveStats,
    Mode,
    all_passed,
    can_switch,
    evaluate,
)
from ..core.risk import RiskLimits, RiskResult, check_orders, is_loosening
from .panel_index import PanelIndex, build_index
from .ports import LedgerPort, MarketView, Strategy

LOOSEN_DELAY = dt.timedelta(hours=24)
#: 実運用で、これより古いデータからは候補を出さない（暦日）
MAX_STALE_DAYS = 5
#: 想定する不利なずれ（片道）。単元未満株の追加スプレッドの仮定と同じ値
ASSUMED_SLIPPAGE = 0.003


class OperationsError(RuntimeError):
    """運用上の理由で、その操作を受け付けない。"""


class StaleDataError(OperationsError):
    """データが古く、実運用の候補には使えない。"""


@dataclass(frozen=True)
class PlanResult:
    as_of: dt.date
    signals: list[dict[str, Any]]
    rejected: list[tuple[str, str]]
    due: bool
    already: bool = False


class OperationsService:
    def __init__(
        self,
        ledger: LedgerPort,
        panel: pd.DataFrame,
        factory: Callable[[dict[str, Any]], Strategy],
        *,
        lot_size: int = 1,
        assumed_slippage: float = ASSUMED_SLIPPAGE,
        market_view: Callable[[PanelIndex, dt.date, int], MarketView] | None = None,
        index: PanelIndex | None = None,
    ) -> None:
        self.ledger = ledger
        # 索引の作成は重い（200万行で数秒）。GUI では作ったものを使い回す
        self.index = index if index is not None else build_index(panel)
        self.factory = factory
        self.lot_size = lot_size
        self.assumed_slippage = assumed_slippage
        if market_view is None:
            from .backtest import PanelMarketView

            market_view = lambda index, day, lot: PanelMarketView(index, day, lot)  # noqa: E731
        self._view = market_view

    # ------------------------------------------------------------------
    # 初期化と設定
    # ------------------------------------------------------------------
    @property
    def initialized(self) -> bool:
        return self.ledger.get_state("strategy") is not None

    def init(
        self,
        *,
        cash: float,
        strategy: str,
        params: dict[str, Any],
        version: str,
        limits: RiskLimits,
        mode: Mode,
        started_on: dt.date,
        at: str,
    ) -> None:
        if self.initialized:
            raise OperationsError("初期化済みです。戦略は凍結されており、変えるなら新しい台帳で始めてください")
        write = lambda k, v, r: self.ledger.set_state(k, v, reason=r, at=at)  # noqa: E731
        write("initial_cash", str(cash), "初期化")
        write("strategy", strategy, "戦略を凍結")
        write("params", json.dumps(params, sort_keys=True), "戦略を凍結")
        write("version", version, "戦略を凍結")
        write("limits", json.dumps(asdict(limits)), "初期化")
        write("mode", str(int(mode)), "初期化")
        write("started_on", started_on.isoformat(), "初期化")
        write("loss_breaches", "0", "初期化")

    @property
    def mode(self) -> Mode:
        return Mode(int(self.ledger.get_state("mode", "0")))

    def set_mode(self, target: Mode, *, reason: str, at: str) -> None:
        ok, why = can_switch(self.mode, target)
        if not ok:
            raise OperationsError(why)
        self.ledger.set_state("mode", str(int(target)), reason=reason, at=at)

    @property
    def limits(self) -> RiskLimits:
        return RiskLimits(**json.loads(self.ledger.get_state("limits", "{}") or "{}"))

    def request_limits(self, new: RiskLimits, *, reason: str, now: dt.datetime) -> str:
        """制限の変更を申請する。厳しくする変更は即時、緩める変更は24時間後。

        返り値は ``"applied"`` か ``"pending"``。
        """
        if not is_loosening(self.limits, new):
            self.ledger.set_state("limits", json.dumps(asdict(new)), reason=reason, at=now.isoformat())
            self.ledger.set_state("pending_limits", "", reason="厳しくする変更を適用", at=now.isoformat())
            return "applied"
        effective = (now + LOOSEN_DELAY).isoformat()
        payload = json.dumps({"limits": asdict(new), "effective_after": effective, "reason": reason})
        self.ledger.set_state("pending_limits", payload, reason=f"緩める変更を申請: {reason}", at=now.isoformat())
        return "pending"

    def apply_pending(self, now: dt.datetime) -> bool:
        raw = self.ledger.get_state("pending_limits", "")
        if not raw:
            return False
        pending = json.loads(raw)
        if now < dt.datetime.fromisoformat(pending["effective_after"]):
            return False
        self.ledger.set_state(
            "limits", json.dumps(pending["limits"]), reason=f"待機後に適用: {pending['reason']}", at=now.isoformat()
        )
        self.ledger.set_state("pending_limits", "", reason="適用済み", at=now.isoformat())
        return True

    # ------------------------------------------------------------------
    # 価格と評価
    # ------------------------------------------------------------------
    def _section(self, day: dt.date) -> pd.DataFrame:
        return self.index.cross_section(day)

    @staticmethod
    def _raw(frame: pd.DataFrame, symbol: str, column: str) -> float | None:
        """調整前の価格。実際に払う金額はこれで決まる。

        調整済みの始値・終値に ``raw_close / close`` を掛けて戻す（分割の取り込みを外す）。
        """
        if frame.empty or symbol not in frame.index:
            return None
        row = frame.loc[symbol]
        value = row.get(column)
        if value is None or value != value:
            return None
        if "raw_close" in frame.columns and row.get("close"):
            return float(value) * float(row["raw_close"]) / float(row["close"])
        return float(value)

    def portfolio(self, upto: dt.date | None = None) -> Portfolio:
        portfolio = Portfolio(cash=float(self.ledger.get_state("initial_cash", "0") or 0))
        for record in self.ledger.fills():
            day = dt.date.fromisoformat(record["date"])
            if upto is not None and day > upto:
                continue
            fill = Fill(
                date=day,
                symbol=record["symbol"],
                side=Side(record["side"]),
                quantity=record["quantity"],
                price=record["price"],
                commission=record["fee"] or 0.0,
                slippage=0.0,
                reason=record.get("note") or "",
            )
            portfolio, _ = apply_fill(portfolio, fill)
        return portfolio

    def mark(self, day: dt.date) -> tuple[float, float]:
        """その日の引け値で評価額を記録する。"""
        frame = self._section(day)
        portfolio = self.portfolio(upto=day)
        value = 0.0
        for position in portfolio.open_positions:
            price = self._raw(frame, position.symbol, "close") or position.average_price
            value += position.quantity * price
        equity = portfolio.cash + value
        self.ledger.save_equity(day.isoformat(), equity, portfolio.cash)
        return equity, portfolio.cash

    # ------------------------------------------------------------------
    # 売買候補
    # ------------------------------------------------------------------
    def _due(self, strategy: Any, dates: list[dt.date], as_of: dt.date) -> bool:
        last = self.ledger.get_state("last_rebalance")
        if not last:
            return True
        last_day = dt.date.fromisoformat(last)
        if last_day not in dates:
            return True
        every = int(getattr(strategy, "rebalance_every", 1) or 1)
        return dates.index(as_of) - dates.index(last_day) >= every

    def plan(
        self,
        as_of: dt.date,
        today: dt.date,
        *,
        strategy: Any = None,
        replay: bool = False,
    ) -> PlanResult:
        """``as_of`` の引け時点の売買候補を作る。

        ``strategy`` を渡すと、その履歴を使い回す（ペーパーの再生用）。
        渡さなければ、毎回新しく作って助走させる（実運用: 毎回プロセスが替わるため）。
        """
        if not self.initialized:
            raise OperationsError("未初期化です。先に init してください")
        if self.mode is Mode.STOP:
            raise OperationsError("停止中です。再開するにはモードを変えてください")
        if self.mode is Mode.SIGNAL and not replay:
            age = (today - as_of).days
            if age > MAX_STALE_DAYS:
                raise StaleDataError(
                    f"データが古すぎます（最新 {as_of}、今日 {today}、{age}日前）。"
                    "実運用の候補には、新しいデータが要ります（J-Quants の Free プランは12週間遅れです）"
                )

        dates = [d for d in self.index.dates if d <= as_of]
        if not dates or dates[-1] != as_of:
            raise OperationsError(f"{as_of} のデータがありません")

        existing = [s for s in self.ledger.signals() if s["as_of"] == as_of.isoformat()]
        if existing and not replay:
            return PlanResult(as_of, existing, [], due=False, already=True)

        params = json.loads(self.ledger.get_state("params", "{}") or "{}")
        persistent = strategy is not None
        if strategy is None:
            strategy = self.factory(params)
            for day in dates[-1 - strategy.history_needed : -1]:
                strategy.observe(self._view(self.index, day, self.lot_size))

        view = self._view(self.index, as_of, self.lot_size)
        portfolio = self.portfolio(upto=as_of)
        due = self._due(strategy, dates, as_of)
        if due:
            strategy.force_rebalance = True
            orders = list(strategy.on_bar(view, portfolio) or [])
            strategy.force_rebalance = False
        else:
            if persistent:
                strategy.observe(view)
            orders = []
        if due and getattr(strategy, "ready", True):
            self.ledger.set_state("last_rebalance", as_of.isoformat(), reason="入れ替え日", at=as_of.isoformat())

        return self._store(as_of, self._check(as_of, orders, portfolio), due)

    def _check(self, as_of: dt.date, orders: list, portfolio: Portfolio) -> RiskResult:
        """注文をリスク制限に照らす。戦略の候補も、外部からの候補も、ここを必ず通る。"""
        frame = self._section(as_of)
        prices = {o.symbol: self._raw(frame, o.symbol, "close") for o in orders}
        holdings = {p.symbol: p.quantity for p in portfolio.open_positions}
        for symbol in holdings:
            prices.setdefault(symbol, self._raw(frame, symbol, "close"))
        equity = portfolio.cash + sum(
            q * (prices.get(s) or 0.0) for s, q in holdings.items()
        )
        curve = self.ledger.equity_curve()
        previous = curve[-2][1] if len(curve) >= 2 else (curve[-1][1] if curve else equity)
        day_return = equity / previous - 1.0 if previous else 0.0

        return check_orders(
            orders,
            prices={s: p for s, p in prices.items() if p},
            holdings=holdings,
            equity=equity,
            day_return=day_return,
            limits=self.limits,
            lot_size=self.lot_size,
        )

    def submit_orders(self, as_of: dt.date, orders: list, *, source: str) -> PlanResult:
        """外部（拡張パーツなど）からの売買候補を受け取る差し込み口。

        **ペーパー運用（モード2）でのみ受け付ける。** 実運用の候補は、凍結した戦略だけが出す。
        受け取った候補は、戦略の候補と同じリスク制限を必ず通り、通ったものだけが台帳に残る。
        理由の先頭に ``source`` を付けて、どこから来た候補かを記録する。
        """
        if not self.initialized:
            raise OperationsError("未初期化です。先に init してください")
        if self.mode is not Mode.PAPER:
            raise OperationsError("外部からの売買候補は、ペーパー運用（モード2）でのみ受け付けます")
        if as_of not in self.index.dates:
            raise OperationsError(f"{as_of} のデータがありません")
        tagged = [Order(o.symbol, o.side, o.quantity, reason=f"[{source}] {o.reason}".strip()) for o in orders]
        return self._store(as_of, self._check(as_of, tagged, self.portfolio(upto=as_of)), due=True)

    def _store(self, as_of: dt.date, result: RiskResult, due: bool) -> PlanResult:
        version = self.ledger.get_state("version", "") or ""
        frame = self._section(as_of)
        signals: list[dict[str, Any]] = []
        for order in result.approved:
            record = {
                "as_of": as_of.isoformat(),
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "ref_price": self._raw(frame, order.symbol, "close"),
                "reason": order.reason,
                "version": version,
                "status": "planned",
            }
            record["id"] = self.ledger.add_signal(record)
            signals.append(record)
        rejected = [(f"{o.symbol} {o.side.value} {o.quantity:g}", why) for o, why in result.rejected]
        if any("日次損失" in why for _, why in result.rejected):
            count = int(self.ledger.get_state("loss_breaches", "0") or 0) + 1
            self.ledger.set_state("loss_breaches", str(count), reason="日次損失の上限に到達", at=as_of.isoformat())
        return PlanResult(as_of, signals, rejected, due)

    # ------------------------------------------------------------------
    # 約定の記録
    # ------------------------------------------------------------------
    def record_fill(
        self,
        *,
        day: dt.date,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        fee: float = 0.0,
        signal_id: int | None = None,
        source: str = "manual",
        note: str = "",
    ) -> dict[str, Any]:
        """実際の約定を記録する。想定の約定価格（約定日の始値）との差も測る。"""
        if side not in ("buy", "sell"):
            raise OperationsError("side は buy か sell")
        if quantity <= 0 or price <= 0:
            raise OperationsError("数量と価格は正の値で入力してください")
        portfolio = self.portfolio(upto=day)
        if side == "sell" and portfolio.quantity(symbol) + 1e-9 < quantity:
            raise OperationsError(f"{symbol} の保有（{portfolio.quantity(symbol):g}）を超える売りです")
        if side == "buy" and price * quantity + fee > portfolio.cash + 1e-6:
            raise OperationsError(f"現金が足りません（{portfolio.cash:,.0f}円）")

        ref_open = self._raw(self._section(day), symbol, "open") if day in self.index.dates else None
        record = {
            "signal_id": signal_id,
            "date": day.isoformat(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fee": fee,
            "source": source,
            "ref_open": ref_open,
            "note": note,
        }
        record["id"] = self.ledger.add_fill(record)
        if signal_id is not None:
            self.ledger.set_signal_status(signal_id, "filled")
        return record

    def paper_fill_pending(self, day: dt.date) -> list[dict[str, Any]]:
        """前日までの候補を、``day`` の始値 ± 想定のずれで仮想的に約定する。売りが先。"""
        frame = self._section(day)
        pending = [s for s in self.ledger.signals("planned") if s["as_of"] < day.isoformat()]
        pending.sort(key=lambda s: (s["side"] != "sell", s["id"]))
        fills = []
        for signal in pending:
            open_price = self._raw(frame, signal["symbol"], "open")
            if not open_price:
                self.ledger.set_signal_status(signal["id"], "unfilled")
                continue
            buying = signal["side"] == "buy"
            price = open_price * (1 + self.assumed_slippage if buying else 1 - self.assumed_slippage)
            quantity = float(signal["quantity"])
            portfolio = self.portfolio(upto=day)
            if buying:
                quantity = float(int(min(quantity, portfolio.cash / price) / self.lot_size) * self.lot_size)
            else:
                quantity = min(quantity, portfolio.quantity(signal["symbol"]))
            if quantity <= 0:
                self.ledger.set_signal_status(signal["id"], "unfilled")
                continue
            fills.append(
                self.record_fill(
                    day=day, symbol=signal["symbol"], side=signal["side"], quantity=quantity,
                    price=price, signal_id=signal["id"], source="paper", note="ペーパー",
                )
            )
        return fills

    def replay(self, start: dt.date, end: dt.date) -> dict[str, Any]:
        """過去のデータを1日ずつ進め、ペーパーで運用の流れを回す。"""
        if self.mode is not Mode.PAPER:
            raise OperationsError("ペーパーのモードでのみ再生できます")
        params = json.loads(self.ledger.get_state("params", "{}") or "{}")
        strategy = self.factory(params)
        days = [d for d in self.index.dates if d <= end]
        first_fill = None
        for day in days:
            if day < start:
                strategy.observe(self._view(self.index, day, self.lot_size))
                continue
            made = self.paper_fill_pending(day)
            first_fill = first_fill or (day if made else None)
            self.mark(day)
            self.plan(day, today=day, strategy=strategy, replay=True)
        return {"days": len([d for d in days if d >= start]), "first_fill": first_fill}

    # ------------------------------------------------------------------
    # 状況と増額の条件
    # ------------------------------------------------------------------
    def stats(self) -> LiveStats:
        curve = self.ledger.equity_curve()
        equity = pd.Series([e for _, e, _ in curve], dtype=float)
        drawdown = float((equity / equity.cummax() - 1).min()) if len(equity) else 0.0

        slips = []
        for f in self.ledger.fills():
            # ペーパーの約定は、想定のずれをそのまま入れているので測る意味がない。
            # 入れると、増額の条件（スリッページ）が必ず合格してしまう
            if f["source"] == "paper":
                continue
            if f["ref_open"]:
                raw = f["price"] / f["ref_open"] - 1.0
                slips.append(raw if f["side"] == "buy" else -raw)
        return LiveStats(
            days=len(curve),
            fills=len(self.ledger.fills()),
            mean_slippage=float(sum(slips) / len(slips)) if slips else None,
            assumed_slippage=self.assumed_slippage,
            max_drawdown=drawdown,
            loss_breaches=int(self.ledger.get_state("loss_breaches", "0") or 0),
        )

    def gates(self, thresholds: GateThresholds = GateThresholds()) -> list[Check]:
        return evaluate(self.stats(), thresholds)

    def status(self, day: dt.date) -> dict[str, Any]:
        portfolio = self.portfolio(upto=day)
        frame = self._section(day) if day in self.index.dates else pd.DataFrame()
        positions = []
        for p in portfolio.open_positions:
            price = self._raw(frame, p.symbol, "close") or p.average_price
            positions.append({"symbol": p.symbol, "quantity": p.quantity, "price": price, "value": p.quantity * price})
        curve = self.ledger.equity_curve()
        return {
            "mode": self.mode,
            "strategy": self.ledger.get_state("strategy"),
            "params": json.loads(self.ledger.get_state("params", "{}") or "{}"),
            "version": self.ledger.get_state("version"),
            "cash": portfolio.cash,
            "equity": curve[-1][1] if curve else portfolio.cash,
            "positions": positions,
            "limits": self.limits,
            "pending_limits": self.ledger.get_state("pending_limits", ""),
            "gates": self.gates(),
            "gates_passed": all_passed(self.gates()),
            "planned": self.ledger.signals("planned"),
            "last_rebalance": self.ledger.get_state("last_rebalance"),
        }
