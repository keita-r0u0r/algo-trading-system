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
