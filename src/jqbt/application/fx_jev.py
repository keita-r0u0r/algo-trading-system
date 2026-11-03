"""Jev（TypeSafe の System One モデル）が売買を判断する作り（判断する者 ``jev``）の、質問と答えの読み方（I/O なし）。

Jev は自由な文を返さない。型の決まった質問（選ぶ・段階・はい/いいえの確からしさ）にだけ答える。

  * 持っていないとき: 通貨ペアごとの「この先4時間の向き」（5段階）・リスクの大きさ（4段階）・決済の手法。
    いちばん傾いた通貨ペアが ``min_tilt`` 以上傾き、確信が ``min_confidence`` 以上なら、その向きに建てる
    （「建てるか」を選ばせると、ほぼ毎回「建てない」を選ぶので、向きの見込みを聞いて機械が選ぶ）
  * 持っているとき: 持ち続ける理由が無くなったか（はいの確からしさ）

**損切り幅は機械が決める**（1時間足のふだんの揺れから。ハーネスと同じ）。リスクの上限・数量の上限・枠は、ほかの判断する者と同じ。
建てたあとの降りる線は、選んだ決済の手法（``fx_exits``）で機械が引く。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import fx_exits

#: 質問の文や読み方を変えたら上げる（記録を版ごとに分けて比べるため）
JEV_VERSION = "jev-v2"
RISK_LEVELS = (0.0025, 0.005, 0.01, 0.02)
OUTLOOK = ["Strongly down over the next 4 hours", "Somewhat down", "Flat / no clear direction", "Somewhat up",
           "Strongly up over the next 4 hours"]
RISK_TEXT = ["Very small risk (0.25% of equity): weak or unclear edge",
             "Small risk (0.5%): some edge",
             "Normal risk (1%): clear edge",
             "Larger risk (2%): strong, clean edge with room to the target"]


@dataclass(frozen=True)
class JevRules:
    min_confidence: float = 0.5
    #: 5段階（0〜4）の見込みが、真ん中（2）からどれだけ離れたら建てるか
    min_tilt: float = 1.0
    close_threshold: float = 0.7
    stop_atr: float = 0.8
    min_stop_pips: float = 12.0
    max_stop_pips: float = 40.0
    hold_minutes: int = 360


def entry_questions(symbols: list[str]) -> dict[str, Any]:
    questions: dict[str, Any] = {
        f"outlook_{s}": {"type": "score", "criteria": OUTLOOK,
                         "instructions": f"Given the market state, where is {s.replace('_', '/')} most likely to go "
                                         "over the next 4 hours?"} for s in symbols}
    questions["risk"] = {"type": "score", "criteria": RISK_TEXT,
                         "instructions": "If the clearest of these moves is traded now, how much of the equity should "
                                         "be at risk on the stop"}
    questions["exit_method"] = {"type": "choice", "criteria": dict(fx_exits.METHODS_EN),
                                "instructions": "Which exit method best fits a trade in the clearest of these moves, "
                                                "given the trend strength, volatility and upcoming events"}
    return questions


def hold_questions() -> dict[str, Any]:
    return {"close": {"type": "noul",
                      "instructions": "The reason to hold the open position is gone: the trend or setup that justified "
                                      "it has broken, or a major event makes holding a poor bet. Close it now."}}


def market_state(features: dict[str, dict[str, Any]], calendar: list[dict[str, Any]], notes: list[dict[str, Any]],
                 position: dict[str, Any] | None = None) -> dict[str, Any]:
    trimmed = {s: {k: v for k, v in f.items() if k not in ("recent_1min_close", "summary")} for s, f in features.items() if f}
    return {"position": position, "market_features": trimmed, "economic_calendar": calendar, "user_notes": notes,
            "units_note": "pips: 1 pip = 0.01 JPY. noise_ratio = move / usual noise for that length (below 1 is noise). "
                          "bollinger_z = distance from the 20-bar mean in standard deviations."}


def stop_pips(atr_1h: float | None, rules: JevRules = JevRules()) -> float:
    base = rules.stop_atr * atr_1h if atr_1h else rules.min_stop_pips
    return round(min(rules.max_stop_pips, max(rules.min_stop_pips, base)), 1)


def read_entry(response: dict[str, Any], rules: JevRules = JevRules()) -> dict[str, Any]:
    """建てる答えを読む。{"open": bool, "symbol", "side", "risk_fraction", "exit_method", "why", "outlooks"}。"""
    answers = response.get("answers") or {}
    outlooks = {k.removeprefix("outlook_"): (v.get("score"), v.get("confidence"))
                for k, v in answers.items() if k.startswith("outlook_") and isinstance(v.get("score"), (int, float))}
    risk_score = (answers.get("risk") or {}).get("score")
    level = (RISK_LEVELS[min(len(RISK_LEVELS) - 1, max(0, round(risk_score)))]
             if isinstance(risk_score, (int, float)) else RISK_LEVELS[0])
    method = (answers.get("exit_method") or {}).get("choice")
    method = method if method in fx_exits.METHODS else "chandelier"
    text = "、".join(f"{s} {sc:.2f}" for s, (sc, _) in outlooks.items())
    out = {"open": False, "risk_fraction": level, "exit_method": method,
           "outlooks": {s: {"score": round(sc, 2), "confidence": c} for s, (sc, c) in outlooks.items()}}
    if not outlooks:
        return {**out, "why": "Jev の答えに見込みが無い。建てない"}
    symbol, (score, confidence) = max(outlooks.items(), key=lambda kv: abs(kv[1][0] - 2))
    why = (f"Jev の4時間の見込み（0=強く下・2=横ばい・4=強く上）: {text}。いちばん傾いたのは {symbol} {score:.2f}"
           f"（確信 {confidence or 0:.0%}）、リスク {level:.2%}、決済の手法 {method}")
    out.update(symbol=symbol, side=1 if score > 2 else -1, tilt=round(abs(score - 2), 2), confidence=confidence)
    if abs(score - 2) < rules.min_tilt:
        return {**out, "why": why + f"。傾きが {rules.min_tilt:g} 未満なので建てない"}
    if not isinstance(confidence, (int, float)) or confidence < rules.min_confidence:
        return {**out, "why": why + f"。確信が {rules.min_confidence:.0%} 未満なので建てない"}
    return {**out, "open": True, "why": why}


def read_hold(response: dict[str, Any], rules: JevRules = JevRules()) -> tuple[bool, float | None]:
    p = ((response.get("answers") or {}).get("close") or {}).get("noul")
    return (isinstance(p, (int, float)) and p >= rules.close_threshold), p
