"""機械の決まり（``fx_setups``）が出した候補を「見送るか」だけ答えさせる仕組みと、画面の切り替え（I/O なし）。

見送りを答えるのは Jev（TypeSafe の System One モデル）か、AI（Gemini）。どちらも、向き・数量・損切りは変えられない。

  * ``off``: 誰にも聞かない。決まりの候補をそのまま建てる
  * ``shadow``: 聞いて記録だけ残す（見送ると言っても建てる）。聞いた場合と聞かない場合を、同じ取引で比べられる
  * ``veto``: 「見送る」の確からしさが ``threshold`` 以上なら建てない

設定は ``state/fx_settings.json``（画面から変える）。判断する者ごとの ON/OFF もここに置く。
OFF にした判断する者は新しく建てない（持っている建玉の決済は続ける）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .fx_exits import METHODS as EXIT_METHODS, METHODS_EN as EXIT_METHODS_EN

MODES = {"off": "聞かない", "shadow": "聞いて記録だけ（建てる）", "veto": "見送ると言ったら建てない"}
PROVIDERS = {"jev": "Jev（TypeSafe）", "gemini": "AI（Gemini）"}
#: 決済の手法を誰が選ぶか（建てるときに1回）
EXIT_SELECTORS = {"fixed": "決まった手法", "ai": "AI（Gemini）が選ぶ", "jev": "Jev が選ぶ"}
#: 画面で ON/OFF できる判断する者
SWITCHABLE = {"ai": "AI が判断（今の作り）", "harness": "ハーネス＋決まりが判断（新しい作り）"}


@dataclass(frozen=True)
class FilterSettings:
    mode: str = "shadow"
    provider: str = "jev"
    threshold: float = 0.6


@dataclass(frozen=True)
class ExitSettings:
    #: 実運用で手法ごとの成績を溜めるため、既定は Jev に選ばせる（選べなければ method）
    selector: str = "jev"
    method: str = "chandelier"


@dataclass(frozen=True)
class FxSettings:
    deciders: dict[str, bool] = field(default_factory=lambda: {name: True for name in SWITCHABLE})
    filter: FilterSettings = FilterSettings()
    updated_at: str = ""
    exits: ExitSettings = ExitSettings()

    def enabled(self, name: str) -> bool:
        return self.deciders.get(name, True)

    def to_dict(self) -> dict[str, Any]:
        return {"deciders": dict(self.deciders), "filter": asdict(self.filter), "exits": asdict(self.exits),
                "updated_at": self.updated_at}


def parse_settings(raw: dict[str, Any] | None) -> FxSettings:
    """置いてある設定を読む。知らない値は既定に戻す（壊れた設定で売買を止めない）。"""
    raw = raw or {}
    deciders = {name: bool((raw.get("deciders") or {}).get(name, True)) for name in SWITCHABLE}
    f = raw.get("filter") or {}
    mode = f.get("mode") if f.get("mode") in MODES else FilterSettings.mode
    provider = f.get("provider") if f.get("provider") in PROVIDERS else FilterSettings.provider
    try:
        threshold = min(0.95, max(0.05, float(f.get("threshold", FilterSettings.threshold))))
    except (TypeError, ValueError):
        threshold = FilterSettings.threshold
    e = raw.get("exits") or {}
    exits = ExitSettings(e.get("selector") if e.get("selector") in EXIT_SELECTORS else ExitSettings.selector,
                         e.get("method") if e.get("method") in EXIT_METHODS else ExitSettings.method)
    return FxSettings(deciders, FilterSettings(mode, provider, threshold), str(raw.get("updated_at", "")), exits)


SKIP_QUESTION = (
    "A disciplined professional FX trader reviewing this state would SKIP the proposed trade. "
    "Reasons to skip include: a high-impact economic release for either currency is imminent or just happened; "
    "price is pressing into a major level (day high/low, previous day high/low, round number) against the trade "
    "with little room before the target; the move looks exhausted or the evidence contradicts the trade direction."
)
QUALITY_LEVELS = [
    "Poor: the evidence contradicts the trade",
    "Weak: mixed evidence",
    "Good: the evidence supports the trade",
    "Excellent: strong, clean evidence with room to the target",
]


def review_state(candidate: dict[str, Any], features: dict[str, Any], calendar: list[dict[str, Any]],
                 notes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """見送りを判断させる材料（候補・特徴量・経済指標・利用者のメモ）。生の1分足の並びは渡さない。"""
    trimmed = {k: v for k, v in features.items() if k not in ("recent_1min_close", "summary")}
    return {"proposed_trade": candidate, "market_features": trimmed, "economic_calendar": calendar,
            "user_notes": notes or [],
            "units_note": "pips: 1 pip = 0.01 JPY. noise_ratio = move size / usual noise for that length "
                          "(below 1 is noise). bollinger_z = distance from the 20-bar mean in standard deviations."}


def jev_questions(*, skip: bool = True, exit_method: bool = False) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    if skip:
        questions["skip"] = {"type": "noul", "instructions": SKIP_QUESTION}
        questions["quality"] = {"type": "score", "instructions": "How well the market state supports the proposed trade",
                                "criteria": QUALITY_LEVELS}
    if exit_method:
        questions["exit_method"] = {
            "type": "choice",
            "instructions": "Which exit method best fits this trade, given the trend strength, volatility and "
                            "upcoming events in the state? The goal is large average wins, not a high win rate.",
            "criteria": dict(EXIT_METHODS_EN)}
    return questions


def jev_exit_choice(response: dict[str, Any]) -> dict[str, Any]:
    """Jev が選んだ決済の手法（選べなければ error）。"""
    answer = (response.get("answers") or {}).get("exit_method") or {}
    method = answer.get("choice")
    if method not in EXIT_METHODS:
        return {"by": "jev", "error": "Jev の答えに決済の手法が無い"}
    return {"by": "jev", "method": method, "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities")}


def jev_verdict(response: dict[str, Any], settings: FilterSettings) -> dict[str, Any]:
    """Jev の答えを、見送るかどうかにする。"""
    answers = response.get("answers") or {}
    skip = answers.get("skip", {}).get("noul")
    quality = answers.get("quality", {})
    if not isinstance(skip, (int, float)):
        return {"provider": "jev", "error": "Jev の答えに skip が無い", "veto": False}
    veto = skip >= settings.threshold
    score = quality.get("score")
    text = (f"見送るべき確からしさ {skip:.0%}（{settings.threshold:.0%}以上で見送り）"
            + (f"、根拠の質 {score:.1f}/3（確信 {quality.get('confidence', 0):.0%}）" if isinstance(score, (int, float)) else ""))
    return {"provider": "jev", "model": response.get("model"), "skip_probability": round(float(skip), 3),
            "quality": score, "quality_confidence": quality.get("confidence"), "veto": veto, "text": text,
            "usage": response.get("usage")}


def gemini_verdict(data: dict[str, Any]) -> dict[str, Any]:
    skip = bool(data.get("skip"))
    return {"provider": "gemini", "veto": skip, "text": ("見送る: " if skip else "通す: ") + str(data.get("reason", ""))}


def gemini_exit_choice(data: dict[str, Any]) -> dict[str, Any]:
    method = data.get("exit_method")
    if method not in EXIT_METHODS:
        return {"by": "ai", "error": "AI の答えに決済の手法が無い"}
    return {"by": "ai", "method": method, "reason": str(data.get("exit_reason", ""))}


def apply(verdict: dict[str, Any] | None, settings: FilterSettings) -> tuple[bool, str]:
    """(建ててよいか, 記録に残す理由)。聞けなかったときは建てる（決まりが本体）。"""
    if settings.mode == "off" or verdict is None:
        return True, ""
    name = PROVIDERS.get(verdict.get("provider", ""), verdict.get("provider", ""))
    if verdict.get("error"):
        return True, f"{name} に聞けなかったので、決まりのとおり建てる（{verdict['error']}）"
    if settings.mode == "shadow":
        return True, f"{name}（記録だけ）: {verdict.get('text', '')}"
    if verdict.get("veto"):
        return False, f"{name} が見送りと判断: {verdict.get('text', '')}"
    return True, f"{name} が通した: {verdict.get('text', '')}"


def jev_cost_usd(usage: dict[str, Any] | None, per_million_input: float = 0.042) -> float:
    """Jev の費用（入力 100万トークンあたり $0.042。出力は無料）。"""
    return round(float((usage or {}).get("input_tokens") or 0) * per_million_input / 1_000_000, 6)
