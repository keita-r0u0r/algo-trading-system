"""建てるときのスプレッド（コスト）を、その都度の値段から判断する（I/O なし）。

一律の幅（以前は1 pips）で切ると、損切りが150 pips で何日も持つ取引まで、朝の数 pips で止めてしまう。
逆に短期の取引には、朝の広がったスプレッドは損切り幅の何割にもなる。そこで建てるたびに次の順で判断する。

  1. **上限**: スプレッドが ``max(1 pip, 損切り幅の cost_share)`` を超えたら建てない（払うコストが損切りに比べて大きすぎる）
  2. **待つか**: 何日も持つ取引（損切りが ``patient_stop_pips`` 以上か、保有の目安が1日以上）は、今のスプレッドを
     **1日を通したふだんの幅**（直近の営業日の中央値。待てば払うことになる幅）と比べ、``wait_ratio`` 倍を超えて広がって
     いれば、急ぐ理由が無いので狭まるのを待つ（待つ間の向きの損得は平均すると0、広がった分は確実に払うコスト）。
     朝7〜9時のように毎日広がる時間帯でも、待てば昼の幅で建てられる
  3. それ以外は建てる。短期の取引は時間が勝負なので、上限の内側なら待たない

判断と理由（コストが損切り幅の何%か・ふだんの幅との比・待てば何 pips 得か）は、判断の記録に残す。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class SpreadRule:
    min_limit_pips: float = 1.0
    cost_share: float = 0.10
    patient_stop_pips: float = 100.0
    wait_ratio: float = 2.0


@dataclass(frozen=True)
class SpreadVerdict:
    ok: bool
    text: str
    spread: float
    normal: float | None
    limit: float


def normal_spread(bars: pd.DataFrame | None, now: dt.datetime, *, same_hour: bool = True) -> float | None:
    """直近の営業日（今日より前）のスプレッドの中央値（pips）。``same_hour`` なら今と同じ時間帯（1時間）だけ。"""
    if bars is None or bars.empty or "spread" not in bars:
        return None
    stamp = pd.Timestamp(now.replace(tzinfo=None))
    past = bars[bars.index < stamp.floor("D")]
    if same_hour:
        hour = past[past.index.hour == stamp.hour]["spread"]
        if len(hour) >= 30:
            return round(float(hour.median()), 2)
    return round(float(past["spread"].median()), 2) if len(past) >= 30 else None


def judge(spread: float, *, stop_pips: float | None, hold_minutes: float | None, normal: float | None,
          typical: float | None = None, rule: SpreadRule = SpreadRule()) -> SpreadVerdict:
    """``normal`` は同じ時間帯のふだんの幅、``typical`` は1日を通したふだんの幅（待てば払う幅）。"""
    stop = float(stop_pips or 0)
    limit = round(max(rule.min_limit_pips, rule.cost_share * stop), 2)
    share = f"損切り幅の{spread / stop:.0%}" if stop else "損切り幅なし"
    usual = f"この時間帯のふだんは {normal:.1f} pips" if normal else "ふだんの幅は不明"
    if spread > limit:
        return SpreadVerdict(False, f"スプレッド {spread:.1f} pips は上限 {limit:g} pips（1 pip か損切り幅の{rule.cost_share:.0%}）を超える"
                                    f"（{share}・{usual}）。コストが大きすぎるので建てない", spread, normal, limit)
    patient = stop >= rule.patient_stop_pips or float(hold_minutes or 0) >= 24 * 60
    base = typical or normal
    if patient and base and spread > rule.wait_ratio * base and spread - base >= 0.5:
        return SpreadVerdict(False, f"スプレッド {spread:.1f} pips は、1日を通したふだん（{base:.1f} pips）の{spread / base:.1f}倍。"
                                    f"何日も持つ取引で急ぐ理由が無いので、狭まるのを待つ（待てば約 {spread - base:.1f} pips 得）",
                             spread, normal, limit)
    return SpreadVerdict(True, f"スプレッド {spread:.1f} pips で建てる（{share}・{usual}）", spread, normal, limit)
