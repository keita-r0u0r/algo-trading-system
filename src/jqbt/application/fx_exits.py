"""建てたあとの決済の手法（I/O なし）。ハーネス＋決まり（``fx_setups``）が、建てるときに1つ選び、記録に残す。

どの手法も、最初の損切りは GMOコイン側に置く（システムが止まっても効く）。ここで決めるのは、そのあと
「どこまで戻ったら降りるか」の線（``exit_level``）。線は損切りより不利な側には動かさない。

  * ``target``: 損切りと利確（損切り幅の3倍）だけ。途中では降りない
  * ``breakeven``: 含み益がふだんの揺れの外（損切り幅の1.5倍）に出たら、線を建値＋コストに上げる。利確は3倍
  * ``percent_peak``: 含み益が損切り幅に届いたら、最大の含み益の半分を失ったところで降りる。利確は2倍
    （揺れの大きさを見ないので、揺れの範囲で振り落とされやすい。比べるために残す）
  * ``chandelier``: 建ててからの最高値（売りなら最安値）から、1時間足のふだんの揺れの ``chandelier_k`` 倍の距離で追いかける
    （LeBeau / Elder のシャンデリア・ストップ）。利確は置かない
  * ``structure``: 直近の5分足の安値（売りなら高値）の少し外で追いかける（相場の節目に置く）。利確は置かない

どの手法を使うかは、決まった1つ（``fixed``）か、建てるときに AI・Jev に選ばせる（``fx_filter`` の設定）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

PIP = 0.01
METHODS = {
    "target": "損切りと利確（3倍）だけ。途中では降りない",
    "breakeven": "含み益が損切り幅の1.5倍に出たら建値に引き上げ、あとは利確（3倍）まで持つ",
    "percent_peak": "含み益が損切り幅に届いたら、最大の含み益の半分を失ったところで降りる（利確2倍）",
    "chandelier": "最高値（売りは最安値）から、1時間足のふだんの揺れの一定倍の距離で追いかける（利確なし）",
    "structure": "直近の5分足の安値（売りは高値）の少し外で追いかける（利確なし）",
}
#: AI・Jev に選ばせるときの説明（英語。Jev は英語の説明で選ぶ）
METHODS_EN = {
    "target": "Fixed stop and a take-profit at 3x the stop. Best when price is likely to travel cleanly to a target.",
    "breakeven": "Move the stop to breakeven after a gain of 1.5x the stop, then hold for 3x. Good for strong trends "
                 "with normal pullbacks.",
    "percent_peak": "Exit after giving back half of the peak gain. Tight; suits quiet, choppy markets.",
    "chandelier": "Trail the stop at a multiple of the hourly ATR below the highest price since entry, no target. "
                  "Best for long, persistent trends.",
    "structure": "Trail the stop just beyond the recent 5-minute swing low/high, no target. Suits orderly trends "
                 "with clear swings.",
}


@dataclass(frozen=True)
class ExitParams:
    #: 手法ごとの利確（損切り幅の何倍。0 は置かない）
    take_r: tuple[tuple[str, float], ...] = (("target", 3.0), ("breakeven", 3.0), ("percent_peak", 2.0),
                                             ("chandelier", 0.0), ("structure", 0.0))
    breakeven_at_r: float = 1.5
    percent_at_r: float = 1.0
    percent_giveback: float = 0.5
    chandelier_k: float = 1.5
    structure_bars: int = 6
    structure_buffer_pips: float = 2.0
    cost_pips: float = 1.0

    def take_for(self, method: str) -> float:
        return dict(self.take_r).get(method, 0.0)


def exit_level(method: str, *, side: int, entry: float, stop_pips: float, since_open: pd.DataFrame,
               atr_pips: float | None, params: ExitParams = ExitParams()) -> float | None:
    """手法 ``method`` の、今の降りる線（値段）。まだ線が無ければ None（最初の損切りだけ）。

    ``since_open`` は建ててから今までの1分足（仲値の high / low）。
    """
    if since_open is None or since_open.empty or stop_pips <= 0:
        return None
    best = float(since_open["high"].max()) if side > 0 else float(since_open["low"].min())
    peak = max(0.0, side * (best - entry) / PIP)
    initial = entry - side * stop_pips * PIP
    level: float | None = None
    if method == "breakeven":
        if peak >= params.breakeven_at_r * stop_pips:
            level = entry + side * params.cost_pips * PIP
    elif method == "percent_peak":
        if peak >= params.percent_at_r * stop_pips:
            level = entry + side * max(params.cost_pips, peak * (1 - params.percent_giveback)) * PIP
    elif method == "chandelier":
        if atr_pips:
            level = best - side * params.chandelier_k * atr_pips * PIP
    elif method == "structure":
        bars5 = since_open.resample("5min").agg({"high": "max", "low": "min"}).dropna()
        if len(bars5) > params.structure_bars:  # 今の5分足は除いて、完成した足の節目を使う
            done = bars5.iloc[-params.structure_bars - 1:-1]
            swing = float(done["low"].min()) if side > 0 else float(done["high"].max())
            level = swing - side * params.structure_buffer_pips * PIP
    if level is None:
        return None
    if side * (level - initial) <= 0:  # 最初の損切りより不利な線は意味が無い
        return None
    return round(level, 3)
