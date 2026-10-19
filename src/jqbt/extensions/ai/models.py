"""選べるモデルの一覧と、どのモデルを使うかの設定。

モデルは自由に選べる。一覧に無いモデル名も指定できるが、料金が分からないので
**一覧で最も高い料金で見積もる**（費用の上限を甘くしないため）。断られたときの自動切り替え
（フォールバック）も、使えるか分からないので付けない。

料金は 100万トークンあたりの米ドル（2026年10月時点の公表値。変わることがあるので公式で確認すること）。
キャッシュの書き込みは入力の1.25倍（5分）。Haiku 5.5 のキャッシュ読み込みは公表値を確認できていないので、入力の1割で見積もる。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    input: float
    output: float
    cache_read: float
    #: 断られたときに、サーバー側で推奨の別モデルに切り替えられるか（fallbacks: "default"）
    fallback: bool
    note: str = ""

    @property
    def cache_write(self) -> float:
        return self.input * 1.25


CATALOG: dict[str, ModelSpec] = {
    m.id: m
    for m in (
        ModelSpec("claude-fable-5-1", "Claude Fable 5.1", 10.00, 50.00, 0.25, True, "最も高性能。最も高い"),
        ModelSpec("claude-opus-5-5", "Claude Opus 5.5", 4.00, 20.00, 0.20, True, "既定。性能と費用の釣り合い"),
        ModelSpec("claude-sonnet-5-5", "Claude Sonnet 5.5", 2.00, 10.00, 0.20, True, "速く、安い"),
        ModelSpec("claude-haiku-5-5", "Claude Haiku 5.5", 0.10, 0.50, 0.01, False, "最も安い。断られたときの切り替えは無い"),
    )
}
DEFAULT_MODEL = "claude-opus-5-5"
ROLES = ("explain", "report", "ideas", "decide")
ROLE_LABELS = {"explain": "A 質問", "report": "B 要約", "ideas": "C 案", "decide": "D 判断"}


def spec(model: str) -> ModelSpec:
    """一覧に無いモデルは、最も高い料金・フォールバックなしで扱う。"""
    if model in CATALOG:
        return CATALOG[model]
    worst = max(CATALOG.values(), key=lambda m: m.output)
    return ModelSpec(model, f"{model}（一覧に無い）", worst.input, worst.output, worst.input, False, "料金は最も高いモデルで見積もる")


def cost_usd(usage: dict[str, int], model: str) -> float:
    """1回の呼び出しの費用（米ドル）。"""
    m = spec(model)
    return (
        usage.get("input_tokens", 0) * m.input
        + usage.get("output_tokens", 0) * m.output
        + usage.get("cache_read_input_tokens", 0) * m.cache_read
        + usage.get("cache_creation_input_tokens", 0) * m.cache_write
    ) / 1_000_000


@dataclass
class ModelChoice:
    """どのモデルを使うか。既定のモデルと、役割ごとの上書き。"""

    default: str = DEFAULT_MODEL
    per_role: dict[str, str] = field(default_factory=dict)

    def model_for(self, role: str) -> str:
        return self.per_role.get(role) or self.default


class ModelSettings:
    """モデルの選択を ``state/ai_settings.json`` に保存する（画面から切り替えられるように）。

    環境変数 ``AI_MODEL`` があれば、保存した既定より優先する（Docker などで固定したいとき）。
    """

    DEFAULT_PATH = Path(__file__).resolve().parents[4] / "state" / "ai_settings.json"

    def __init__(self, path: Path | str = DEFAULT_PATH, environ: dict[str, str] | None = None) -> None:
        self.path = Path(path)
        self._environ = environ

    def load(self) -> ModelChoice:
        choice = ModelChoice()
        if self.path.exists():
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            choice = ModelChoice(default=raw.get("default") or DEFAULT_MODEL, per_role=dict(raw.get("per_role") or {}))
        import os

        environ = os.environ if self._environ is None else self._environ
        if environ.get("AI_MODEL"):
            choice.default = environ["AI_MODEL"].strip()
        choice.per_role = {r: m for r, m in choice.per_role.items() if r in ROLES and m}
        return choice

    def save(self, choice: ModelChoice) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(choice), ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
