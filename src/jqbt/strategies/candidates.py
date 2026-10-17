"""日足で検証できる、有名な手法の候補。

**これらは「試してみる候補」であって、効くと分かっているものではない。**
パラメータの格子は小さく固定してある（``strategies/__init__.py``）。
軸を足すと試行回数 N が掛け算で増え、選択バイアスの補正が厳しくなる。

共通の設計:
  * 戦略は「その日までの世界」（MarketView）しか見ない。履歴は自分で積み上げる
    （``RollingWindow``）。毎日 ``view.history()`` を呼ぶと全期間の再構築で重いため
  * 買える銘柄（1単元の金額が予算内）と、流動性のある銘柄だけを対象にする
  * 判断は当日の終値まで、執行は翌営業日の寄り（エンジンの約束）
"""

from __future__ import annotations

import functools
import warnings

import numpy as np

from ..application.ports import MarketView
from ..core.contracts import Order, Portfolio, Side
from .base import size_by_cash

#: 直近20日の売買代金の中央値がこれ未満の銘柄は対象外（円）。
#: 薄い銘柄はスプレッドの前提（片道0.3%）が成り立たない
DEFAULT_MIN_TURNOVER = 30_000_000.0
LIQUIDITY_DAYS = 20


def _quiet(method):
    """全部が欠損の銘柄列で出る numpy の警告を抑える。欠損は NaN で正しく扱っている。"""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return method(*args, **kwargs)

    return wrapper


def raw_price(view: MarketView, symbol: str) -> float | None:
    """調整前の終値。数量の計算に使う（実際に払う金額はこの価格で決まる）。"""
    frame = view.today()
    if frame.empty or symbol not in frame.index:
        return None
    column = "raw_close" if "raw_close" in frame.columns else "close"
    value = frame.at[symbol, column]
    return float(value) if value == value and value > 0 else None


class RollingWindow:
    """直近 ``length`` 営業日の終値と売買代金を、銘柄×日の行列で持つ。

    銘柄は途中で増えてよい（新規上場）。欠けている日は NaN。
    """

    def __init__(self, length: int) -> None:
        self.length = length
        self.days = 0
        self._cursor = 0
        self._col: dict[str, int] = {}
        self._symbols: list[str] = []
        self._close = np.full((length, 512), np.nan)
        self._turnover = np.full((length, 512), np.nan)
        #: 調整前の最新の終値。買えるかの判定に使う（下記）
        self._raw_last = np.full(512, np.nan)

    def push(self, view: MarketView) -> None:
        frame = view.today()
        row_close = np.full(self._close.shape[1], np.nan)
        row_raw = np.full(self._close.shape[1], np.nan)
        row_turnover = np.full(self._close.shape[1], np.nan)
        if not frame.empty:
            index = []
            for symbol in frame.index:
                symbol = str(symbol)
                position = self._col.get(symbol)
                if position is None:
                    position = len(self._symbols)
                    self._col[symbol] = position
                    self._symbols.append(symbol)
                    if position >= self._close.shape[1]:
                        self._grow()
                        row_close = np.pad(row_close, (0, self._close.shape[1] - row_close.size), constant_values=np.nan)
                        row_turnover = np.pad(row_turnover, (0, self._close.shape[1] - row_turnover.size), constant_values=np.nan)
                        row_raw = np.pad(row_raw, (0, self._close.shape[1] - row_raw.size), constant_values=np.nan)
                index.append(position)
            row_close[index] = frame["close"].to_numpy(dtype=float)
            row_turnover[index] = frame["turnover"].to_numpy(dtype=float)
            raw = frame["raw_close"] if "raw_close" in frame.columns else frame["close"]
            row_raw[index] = raw.to_numpy(dtype=float)
            self._raw_last = row_raw
        self._close[self._cursor, : row_close.size] = row_close
        self._turnover[self._cursor, : row_turnover.size] = row_turnover
        self._cursor = (self._cursor + 1) % self.length
        self.days += 1

    def raw_last(self) -> np.ndarray:
        """調整前の最新の終値（銘柄の並びは closes() と同じ）。

        買えるかの判定は **調整前** の価格で行う。調整済みの終値は、後から起きた
        株式分割で過去の価格が割り引かれる。それで判定すると、当時は買えなかった
        銘柄を「買える」と見なし、しかも分割は上昇のあとに起きやすいので、
        将来を知っているのと同じ偏りが乗る。
        """
        n = len(self._symbols)
        return self._raw_last[:n]

    def _grow(self) -> None:
        extra = self._close.shape[1]
        pad = np.full((self.length, extra), np.nan)
        self._close = np.hstack([self._close, pad])
        self._turnover = np.hstack([self._turnover, pad.copy()])

    @property
    def ready(self) -> bool:
        return self.days >= self.length

    @property
    def symbols(self) -> list[str]:
        return self._symbols

    def closes(self) -> np.ndarray:
        """古い順の (length, 銘柄数)。"""
        n = len(self._symbols)
        return np.roll(self._close[:, :n], -self._cursor, axis=0)

    def turnovers(self) -> np.ndarray:
        n = len(self._symbols)
        return np.roll(self._turnover[:, :n], -self._cursor, axis=0)


class _Candidate:
    """共通処理: 履歴の積み上げ、対象銘柄の絞り込み、売買注文の組み立て。"""

    name = "candidate"

    def __init__(
        self,
        warmup: int,
        max_positions: int = 5,
        min_turnover: float = DEFAULT_MIN_TURNOVER,
    ) -> None:
        self.warmup = warmup
        self.max_positions = max_positions
        self.min_turnover = min_turnover
        self._budget = 0.0
        self._window = RollingWindow(max(warmup, LIQUIDITY_DAYS))
        #: 運用では「今日が入れ替え日か」を台帳が決める。True なら予定表を無視して判断する
        self.force_rebalance = False
        self.rebalance_every = 1

    @property
    def history_needed(self) -> int:
        """判断の前に積んでおくべき日数（当日を含まない）。"""
        return self._window.length - 1

    @property
    def ready(self) -> bool:
        """助走が終わり、判断できる状態か。"""
        return self._window.days >= self.warmup

    def observe(self, view: MarketView) -> None:
        """判断せずに履歴だけ積む。運用で、毎回プロセスを立ち上げ直すときの助走に使う。"""
        self._window.push(view)

    # ------------------------------------------------------------------
    def _eligible(self, view: MarketView, closes: np.ndarray, turnovers: np.ndarray) -> np.ndarray:
        """買えて、流動性があり、価格が揃っている銘柄の真偽配列。"""
        last = closes[-1]
        budget = self._budget
        with np.errstate(invalid="ignore"):
            affordable = self._window.raw_last() * view.lot_size <= budget
            liquid = np.nanmedian(turnovers[-LIQUIDITY_DAYS:], axis=0) >= self.min_turnover
            complete = ~np.isnan(closes[-self.warmup :]).any(axis=0)
        return affordable & liquid & complete & ~np.isnan(last)

    def _orders(
        self,
        view: MarketView,
        portfolio: Portfolio,
        targets: list[str],
        reason: str,
    ) -> list[Order]:
        """保有を ``targets`` に合わせる。外れた銘柄を売り、足りない枠を買う。"""
        orders: list[Order] = []
        target_set = set(targets)
        for position in portfolio.open_positions:
            if position.symbol not in target_set:
                orders.append(Order(position.symbol, Side.SELL, position.quantity, reason=f"{reason}: 対象外"))

        held = {p.symbol for p in portfolio.open_positions}
        to_buy = [s for s in targets if s not in held]
        if not to_buy:
            return orders

        prices = {s: view.price(s) for s in held}
        equity = portfolio.cash + sum(
            p.quantity * (prices.get(p.symbol) or p.average_price) for p in portfolio.open_positions
        )
        budget = equity / max(self.max_positions, 1)
        for symbol in to_buy:
            price = raw_price(view, symbol)
            if not price:
                continue
            quantity = size_by_cash(budget, price, lot=view.lot_size)
            if quantity > 0:
                orders.append(Order(symbol, Side.BUY, quantity, reason=reason))
        return orders

    def _set_budget(self, view: MarketView, portfolio: Portfolio) -> None:
        prices = {p.symbol: view.price(p.symbol) or p.average_price for p in portfolio.open_positions}
        equity = portfolio.cash + sum(p.quantity * prices[p.symbol] for p in portfolio.open_positions)
        self._budget = equity / max(self.max_positions, 1)


class _Ranked(_Candidate):
    """スコアの高い順に ``max_positions`` 銘柄を持ち、一定間隔で入れ替える。"""

    def __init__(self, warmup: int, rebalance_every: int, **kwargs) -> None:
        super().__init__(warmup, **kwargs)
        self.rebalance_every = rebalance_every

    def score(self, closes: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    @_quiet
    def on_bar(self, view: MarketView, portfolio: Portfolio) -> list[Order]:
        self._window.push(view)
        if self._window.days < self.warmup:
            return []
        if not self.force_rebalance and (self._window.days - self.warmup) % self.rebalance_every != 0:
            return []

        self._set_budget(view, portfolio)
        closes = self._window.closes()
        eligible = self._eligible(view, closes, self._window.turnovers())
        scores = np.where(eligible, self.score(closes), np.nan)
        valid = np.flatnonzero(~np.isnan(scores))
        if valid.size == 0:
            return self._orders(view, portfolio, [], self.name)
        top = valid[np.argsort(-scores[valid])[: self.max_positions]]
        targets = [self._window.symbols[i] for i in top]
        return self._orders(view, portfolio, targets, self.name)


class Momentum(_Ranked):
    """過去 ``lookback`` 日で強かった銘柄を持つ（直近 ``skip`` 日は除く）。

    直近を除くのは、短期では逆に戻る傾向（短期逆張り）と混ざるのを避けるため。
    """

    def __init__(self, lookback: int = 120, skip: int = 20, rebalance_every: int = 20, **kwargs) -> None:
        super().__init__(warmup=lookback + 1, rebalance_every=rebalance_every, **kwargs)
        self.lookback = lookback
        self.skip = skip
        self.name = f"momentum_{lookback}_{skip}"

    def score(self, closes: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return closes[-1 - self.skip] / closes[-1 - self.lookback] - 1.0


class Reversal(_Ranked):
    """直近 ``days`` 日で最も下げた銘柄を買い、``hold`` 日持つ。"""

    def __init__(self, days: int = 5, hold: int = 5, **kwargs) -> None:
        super().__init__(warmup=days + 1, rebalance_every=hold, **kwargs)
        self.days = days
        self.name = f"reversal_{days}_{hold}"

    def score(self, closes: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return -(closes[-1] / closes[-1 - self.days] - 1.0)


class LowVolatility(_Ranked):
    """直近 ``lookback`` 日の値動きが最も小さい銘柄を持つ。"""

    def __init__(self, lookback: int = 60, rebalance_every: int = 20, **kwargs) -> None:
        super().__init__(warmup=lookback + 1, rebalance_every=rebalance_every, **kwargs)
        self.lookback = lookback
        self.name = f"lowvol_{lookback}"

    def score(self, closes: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            returns = np.diff(closes[-self.lookback - 1 :], axis=0) / closes[-self.lookback - 1 : -1]
            volatility = np.nanstd(returns, axis=0)
        # 値動きがほぼ無い銘柄は、約定しない・気配だけの銘柄の疑いがあるので除く
        volatility = np.where(volatility > 1e-6, volatility, np.nan)
        return -volatility


class Breakout(_Candidate):
    """直近 ``entry`` 日の高値を終値で上抜けたら買い、``exit`` 日の安値を割ったら売る。"""

    def __init__(self, entry: int = 55, exit_: int | None = None, **kwargs) -> None:
        super().__init__(warmup=entry + 1, **kwargs)
        self.entry = entry
        self.exit = exit_ or max(entry // 2, 5)
        self.name = f"breakout_{entry}_{self.exit}"

    @_quiet
    def on_bar(self, view: MarketView, portfolio: Portfolio) -> list[Order]:
        self._window.push(view)
        if self._window.days < self.warmup:
            return []

        self._set_budget(view, portfolio)
        closes = self._window.closes()
        symbols = self._window.symbols
        index = {s: i for i, s in enumerate(symbols)}
        last = closes[-1]

        orders: list[Order] = []
        holding = set()
        for position in portfolio.open_positions:
            i = index.get(position.symbol)
            if i is None:
                continue
            low = np.nanmin(closes[-1 - self.exit : -1, i])
            if last[i] < low:
                orders.append(Order(position.symbol, Side.SELL, position.quantity, reason=f"{self.name}: 下抜け"))
            else:
                holding.add(position.symbol)

        slots = self.max_positions - len(holding)
        if slots <= 0:
            return orders

        eligible = self._eligible(view, closes, self._window.turnovers())
        with np.errstate(invalid="ignore"):
            high = np.nanmax(closes[-1 - self.entry : -1], axis=0)
            broke = eligible & (last > high)
            strength = np.where(broke, last / high - 1.0, np.nan)
        candidates = np.flatnonzero(~np.isnan(strength))
        candidates = candidates[np.argsort(-strength[candidates])]
        picked = [symbols[i] for i in candidates if symbols[i] not in holding][:slots]

        budget = self._budget
        for symbol in picked:
            price = raw_price(view, symbol)
            quantity = size_by_cash(budget, price, lot=view.lot_size) if price else 0
            if quantity > 0:
                orders.append(Order(symbol, Side.BUY, quantity, reason=f"{self.name}: 上抜け"))
        return orders


class RegimeHold(_Candidate):
    """売買代金の大きい銘柄を持ち、地合いが悪いときは持たない。

    地合い = 全銘柄の等ウェイト平均リターンの累積が、``ma`` 日移動平均より上かどうか。
    ``ma=0`` は地合いを見ない（常に持つ）。これは候補であると同時に、
    「何も工夫しない保有」という基準線でもある。
    """

    def __init__(self, ma: int = 50, rebalance_every: int = 20, **kwargs) -> None:
        super().__init__(warmup=max(ma, LIQUIDITY_DAYS) + 1, **kwargs)
        self.ma = ma
        self.rebalance_every = rebalance_every
        self.name = f"regime_hold_{ma}"

    def _risk_on(self, closes: np.ndarray) -> bool:
        if self.ma == 0:
            return True
        with np.errstate(invalid="ignore", divide="ignore"):
            daily = np.diff(closes, axis=0) / closes[:-1]
        # 銘柄ごとの極端な値（分割の取り込み漏れなど）が地合いを歪めないよう丸める
        daily = np.clip(np.where(np.isfinite(daily), daily, np.nan), -0.2, 0.2)
        market = np.nanmean(daily, axis=1)
        index = np.cumprod(1.0 + np.nan_to_num(market))
        return bool(index[-1] > index[-self.ma :].mean())

    @_quiet
    def on_bar(self, view: MarketView, portfolio: Portfolio) -> list[Order]:
        self._window.push(view)
        if self._window.days < self.warmup:
            return []
        if not self.force_rebalance and (self._window.days - self.warmup) % self.rebalance_every != 0:
            return []

        self._set_budget(view, portfolio)
        closes = self._window.closes()
        if not self._risk_on(closes):
            return self._orders(view, portfolio, [], self.name)

        eligible = self._eligible(view, closes, self._window.turnovers())
        liquidity = np.where(eligible, np.nanmedian(self._window.turnovers()[-LIQUIDITY_DAYS:], axis=0), np.nan)
        valid = np.flatnonzero(~np.isnan(liquidity))
        top = valid[np.argsort(-liquidity[valid])[: self.max_positions]]
        return self._orders(view, portfolio, [self._window.symbols[i] for i in top], self.name)
