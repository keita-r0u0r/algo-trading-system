"""FX の日中売買の戦略（スキャルピング・デイトレ）。

**これらは試す候補であって、効くと分かっているものではない。**
ダミーデータの上で勝つかどうかは、データの作り方で決まる（``fx_synthetic``）。

戦略は足の終値で判断し、``FxSignal``（望む建玉の向き、損切り・利確の pips）を返す。
数量・約定・ロスカットはエンジンの仕事。
"""

from __future__ import annotations

import numpy as np

from ..application.fx_backtest import FxView
from ..core.fx.model import FxPosition, FxSignal


def _direction(position: FxPosition) -> int:
    return 0 if not position.is_open else (1 if position.is_long else -1)


class EmaCross:
    """短期と長期の指数移動平均の交差で、順張り。交差のたびに向きを入れ替える。"""

    def __init__(self, fast: int = 5, slow: int = 20, stop_pips: float = 8, take_pips: float = 12) -> None:
        self.fast, self.slow = fast, slow
        self.stop_pips, self.take_pips = stop_pips, take_pips
        self.name = f"ema_cross_{fast}_{slow}"
        self._alpha_fast, self._alpha_slow = 2 / (fast + 1), 2 / (slow + 1)
        self._ema_fast = self._ema_slow = None
        self._seen = 0

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None:
        price = view.close[-1]
        if self._ema_fast is None:
            self._ema_fast = self._ema_slow = price
        previous = self._ema_fast - self._ema_slow
        self._ema_fast += self._alpha_fast * (price - self._ema_fast)
        self._ema_slow += self._alpha_slow * (price - self._ema_slow)
        self._seen += 1
        if self._seen < self.slow:
            return None
        current = self._ema_fast - self._ema_slow
        if previous <= 0 < current and _direction(position) != 1:
            return FxSignal(1, self.stop_pips, self.take_pips, "EMA上抜け")
        if previous >= 0 > current and _direction(position) != -1:
            return FxSignal(-1, self.stop_pips, self.take_pips, "EMA下抜け")
        return None


class BollingerRevert:
    """平均から大きく離れたら逆張りし、平均に戻ったら手仕舞う。"""

    def __init__(self, window: int = 20, z: float = 2.0, stop_pips: float = 10) -> None:
        self.window, self.z, self.stop_pips = window, z, stop_pips
        self.name = f"bollinger_{window}_{z:g}"

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None:
        if len(view.close) < self.window:
            return None
        recent = view.close[-self.window :]
        mean, std = recent.mean(), recent.std()
        if std <= 0:
            return None
        score = (view.close[-1] - mean) / std
        held = _direction(position)
        if held == 1 and score >= 0:
            return FxSignal(0, reason="平均へ戻った")
        if held == -1 and score <= 0:
            return FxSignal(0, reason="平均へ戻った")
        if held == 0 and score <= -self.z:
            return FxSignal(1, self.stop_pips, None, "下に離れた")
        if held == 0 and score >= self.z:
            return FxSignal(-1, self.stop_pips, None, "上に離れた")
        return None


class RangeBreakout:
    """直近 ``window`` 本の高値・安値を終値で抜けたら、その向きに入る。"""

    def __init__(self, window: int = 30, stop_pips: float = 8, take_pips: float = 16) -> None:
        self.window, self.stop_pips, self.take_pips = window, stop_pips, take_pips
        self.name = f"breakout_{window}"

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None:
        if len(view.close) <= self.window or position.is_open:
            return None
        high = view.high[-1 - self.window : -1].max()
        low = view.low[-1 - self.window : -1].min()
        if view.close[-1] > high:
            return FxSignal(1, self.stop_pips, self.take_pips, "高値を上抜け")
        if view.close[-1] < low:
            return FxSignal(-1, self.stop_pips, self.take_pips, "安値を下抜け")
        return None


class RandomFx:
    """ランダムに入る。優位性の基準線（これと区別がつかなければ、優位性は無い）。"""

    def __init__(self, probability: float = 0.01, stop_pips: float = 8, take_pips: float = 12, seed: int = 0) -> None:
        self.probability, self.stop_pips, self.take_pips = probability, stop_pips, take_pips
        self.name = "random_fx"
        self._rng = np.random.default_rng(seed)

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None:
        if position.is_open or self._rng.random() > self.probability:
            return None
        side = 1 if self._rng.random() < 0.5 else -1
        return FxSignal(side, self.stop_pips, self.take_pips, "ランダム")


# --- 取引の少ない候補（1回で 1 pip を大きく超える値幅を狙う） ------------------------
# 実データの検証で、1分足で頻繁に売買するとコスト（往復約1 pip）に負けると分かった。
# ここの数値は **結果を見る前に決めて固定したもの**。結果を見て変えたら、別の候補として試行回数に数える。
# 時刻は日本時間（GMOコインの足の時刻）。


def gotobi_days(year: int, month: int) -> set:
    """ゴトー日（5・10・15・20・25・30日。2月は月末）。土日なら前の金曜。祝日は考えない。"""
    import calendar
    import datetime as dt

    last = calendar.monthrange(year, month)[1]
    days = set()
    for d in (5, 10, 15, 20, 25, min(30, last)):
        day = dt.date(year, month, d)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        days.add(day)
    return days


class GotobiFix:
    """ゴトー日の朝、仲値（9:55）に向けたドル買いに乗る。9:00 に買い、9:55 に手仕舞う。"""

    def __init__(self, stop_pips: float = 20) -> None:
        self.stop_pips = stop_pips
        self.name = "gotobi_fix"
        self._month: tuple[int, int] | None = None
        self._days: set = set()

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None:
        t = view.time
        if self._month != (t.year, t.month):
            self._month, self._days = (t.year, t.month), gotobi_days(t.year, t.month)
        if position.is_open:
            if (t.hour == 9 and t.minute >= 54) or t.hour >= 10:
                return FxSignal(0, reason="仲値の後")
            return None
        if t.hour == 9 and t.minute == 0 and t.date() in self._days:
            return FxSignal(1, self.stop_pips, None, "ゴトー日の朝")
        return None


class LondonBreakout:
    """東京時間（9:00〜14:59）の高値・安値を、ロンドンの始まり（16:00〜18:59）に終値で抜けたら、その向きに入る。

    1日1回まで。損切り20・利確40 pips。残っていれば深夜1時に手仕舞う（朝のスプレッドが広がる前）。
    """

    def __init__(self, stop_pips: float = 20, take_pips: float = 40) -> None:
        self.stop_pips, self.take_pips = stop_pips, take_pips
        self.name = "london_breakout"
        self._date = None
        self._high = self._low = None
        self._traded = False

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None:
        t = view.time
        if position.is_open:
            return FxSignal(0, reason="深夜の手仕舞い") if 1 <= t.hour < 9 else None
        if t.date() != self._date:
            self._date, self._high, self._low, self._traded = t.date(), None, None, False
        if 9 <= t.hour < 15:
            hi, lo = view.high[-1], view.low[-1]
            self._high = hi if self._high is None else max(self._high, hi)
            self._low = lo if self._low is None else min(self._low, lo)
            return None
        if 16 <= t.hour < 19 and not self._traded and self._high is not None:
            if view.close[-1] > self._high:
                self._traded = True
                return FxSignal(1, self.stop_pips, self.take_pips, "東京の高値を上抜け")
            if view.close[-1] < self._low:
                self._traded = True
                return FxSignal(-1, self.stop_pips, self.take_pips, "東京の安値を下抜け")
        return None


class HourlyBreakout:
    """1時間足の終値が、直近20本の高値・安値を抜けたら、その向きに入る（ドテン）。損切り50 pips。日をまたいで持つ。"""

    def __init__(self, window: int = 20, stop_pips: float = 50) -> None:
        self.window, self.stop_pips = window, stop_pips
        self.name = f"hourly_breakout_{window}"
        self._hours: list[tuple[float, float]] = []
        self._hour = None
        self._high = self._low = None

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None:
        t = view.time
        key = (t.date(), t.hour)
        if key != self._hour:
            self._hour, self._high, self._low = key, view.high[-1], view.low[-1]
        else:
            self._high, self._low = max(self._high, view.high[-1]), min(self._low, view.low[-1])
        if t.minute != 59:
            return None
        close, previous = view.close[-1], self._hours[-self.window :]
        self._hours.append((self._high, self._low))
        if len(previous) < self.window:
            return None
        held = _direction(position)
        if close > max(h for h, _ in previous) and held != 1:
            return FxSignal(1, self.stop_pips, None, "1時間足の高値を上抜け")
        if close < min(lo for _, lo in previous) and held != -1:
            return FxSignal(-1, self.stop_pips, None, "1時間足の安値を下抜け")
        return None


class BuyAndHoldFx:
    """最初の足で買って、期間の終わりまで持つ。スワップを受け取る。**比べる基準**（候補ではない）。"""

    name = "buy_and_hold"

    def decide(self, view: FxView, position: FxPosition) -> FxSignal | None:
        return None if position.is_open else FxSignal(1, reason="買って持つ")
