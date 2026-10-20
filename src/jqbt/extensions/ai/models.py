"""選べるモデルの一覧と、どのモデルを使うかの設定。

## モデルの呼び方

モデルは「呼び出し先:モデルID」の組で表す（キー）。同じ Claude でも、呼び出し先で認証・請求・機能が違うため。

| 呼び出し先 | 中身 | 認証 |
|---|---|---|
| ``vertex``    | Google Cloud Vertex AI（Claude / Gemini） | gcloud のログイン（GCP_PROJECT） |
| ``anthropic`` | Anthropic の API を直接 | Anthropic の APIキー |

呼び出し先を足すとき（例: ローカルの Ollama）は、ここの ``PROVIDERS`` と ``providers.py`` に1つずつ足す。

## 一覧

既定の一覧（``DEFAULTS``）に、利用者が画面から足したモデル（``state/ai_models.json``）を重ねる。
一覧に無いキーも使えるが、料金が分からないので **一覧で最も高い料金で見積もり**、切り替えも付けない。

料金は 100万トークンあたりの米ドル。Vertex AI は 2026年10月時点の公式の料金表（global エンドポイント）。
お試し価格のある Gemini は、費用の上限を甘くしないよう **お試し期間の後の料金** を入れている。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

STATE = Path(__file__).resolve().parents[4] / "state"

PROVIDERS = {
    "vertex": "Vertex AI",
    "anthropic": "Anthropic（直接）",
}
#: Claude / Gemini のどちらとして呼ぶか（Vertex AI では SDK が違う）
FAMILIES = ("claude", "gemini")


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    id: str
    label: str
    family: str
    input: float
    output: float
    cache_read: float
    #: 断られたときに、サーバー側で推奨の別モデルに切り替えられるか（Anthropic 直接の一部のモデルだけ）
    server_fallback: bool = False
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"

    @property
    def cache_write(self) -> float:
        return self.input * 1.25


def _claude(provider: str, id_: str, label: str, inp: float, out: float, cache: float, fallback: bool, note: str) -> ModelSpec:
    return ModelSpec(provider, id_, label, "claude", inp, out, cache, fallback, note)


DEFAULTS: tuple[ModelSpec, ...] = (
    # --- Vertex AI（Claude）。サーバー側の切り替えは無い（自前の切り替えの順番を使う） ---
    _claude("vertex", "claude-fable-5-1", "Claude Fable 5.1", 10.00, 50.00, 0.25, False,
            "最も高性能。最も高い。Anthropic 向けのデータ共有の設定が必要"),
    _claude("vertex", "claude-opus-5-5", "Claude Opus 5.5", 4.00, 20.00, 0.20, False, "既定。性能と費用の釣り合い"),
    _claude("vertex", "claude-sonnet-5-5", "Claude Sonnet 5.5", 2.00, 10.00, 0.10, False, "速く、安い"),
    _claude("vertex", "claude-haiku-5-5", "Claude Haiku 5.5", 0.10, 0.50, 0.01, False, "最も安い（入力10万トークンまでの料金）"),
    # --- Vertex AI（Gemini）。モデルIDは 2026-10-20 に実際の呼び出しで確認済み ---
    ModelSpec("vertex", "gemini-4-argon", "Gemini 4 Argon", "gemini", 4.00, 20.00, 0.20, False,
              "12/1以降の料金で見積もり（それまでは半額）"),
    ModelSpec("vertex", "gemini-3.8-flash", "Gemini 3.8 Flash", "gemini", 1.50, 7.50, 0.15, False,
              "2027年以降の料金で見積もり（それまでは半額）"),
    ModelSpec("vertex", "gemini-3.1-pro-preview", "Gemini 3.1 Pro（プレビュー）", "gemini", 2.00, 12.00, 0.20, False,
              "入力20万トークンまでの料金"),
    ModelSpec("vertex", "gemini-2.5-flash", "Gemini 2.5 Flash", "gemini", 0.30, 2.50, 0.03, False,
              "2.0 Flash（提供終了）の後継"),
    ModelSpec("vertex", "gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", "gemini", 0.10, 0.40, 0.01, False,
              "最も安い Gemini"),
    # --- Anthropic 直接。断られたときのサーバー側の切り替えがある（Haiku を除く） ---
    _claude("anthropic", "claude-fable-5-1", "Claude Fable 5.1", 10.00, 50.00, 0.25, True, ""),
    _claude("anthropic", "claude-opus-5-5", "Claude Opus 5.5", 4.00, 20.00, 0.20, True, ""),
    _claude("anthropic", "claude-sonnet-5-5", "Claude Sonnet 5.5", 2.00, 10.00, 0.20, True, ""),
    _claude("anthropic", "claude-haiku-5-5", "Claude Haiku 5.5", 0.10, 0.50, 0.01, False, ""),
)
DEFAULT_MODEL = "vertex:claude-opus-5-5"
ROLES = ("explain", "report", "ideas", "decide", "fx")
ROLE_LABELS = {"explain": "A 質問", "report": "B 要約", "ideas": "C 案", "decide": "D 判断", "fx": "E FX の判断"}


def normalize(key: str) -> str:
    """呼び出し先の無い古い書き方（claude-opus-5-5）は、Anthropic 直接として読む。"""
    key = (key or "").strip()
    return key if ":" in key else f"anthropic:{key}"


def split(key: str) -> tuple[str, str]:
    provider, _, model_id = normalize(key).partition(":")
    return provider, model_id


class ModelCatalog:
    """既定の一覧に、利用者が足したモデル（state/ai_models.json）を重ねたもの。"""

    def __init__(self, path: Path | str = STATE / "ai_models.json") -> None:
        self.path = Path(path)

    def custom(self) -> list[ModelSpec]:
        if not self.path.exists():
            return []
        names = {f.name for f in fields(ModelSpec)}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [ModelSpec(**{k: v for k, v in item.items() if k in names}) for item in raw]

    def all(self) -> dict[str, ModelSpec]:
        specs = {m.key: m for m in DEFAULTS}
        specs.update({m.key: m for m in self.custom()})
        return specs

    def add(self, spec: ModelSpec) -> None:
        """モデルを足す（同じキーは上書き）。料金がマイナスなど、明らかな誤りは受け付けない。"""
        if spec.provider not in PROVIDERS:
            raise ValueError(f"呼び出し先は {', '.join(PROVIDERS)} のどれかです")
        if spec.family not in FAMILIES:
            raise ValueError(f"種類は {', '.join(FAMILIES)} のどちらかです")
        if not spec.id.strip() or min(spec.input, spec.output, spec.cache_read) < 0:
            raise ValueError("モデルIDと、0以上の料金を入れてください")
        items = [m for m in self.custom() if m.key != spec.key] + [spec]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(m) for m in items], ensure_ascii=False, indent=1), encoding="utf-8")


def spec(key: str, catalog: ModelCatalog | None = None) -> ModelSpec:
    """一覧に無いモデルは、最も高い料金・切り替えなしで扱う。"""
    key = normalize(key)
    known = (catalog or ModelCatalog()).all()
    if key in known:
        return known[key]
    provider, model_id = split(key)
    worst = max(known.values(), key=lambda m: m.output)
    family = "gemini" if model_id.startswith("gemini") else "claude"
    return ModelSpec(provider, model_id, f"{model_id}（一覧に無い）", family,
                     worst.input, worst.output, worst.input, False, "料金は最も高いモデルで見積もる")


def cost_usd(usage: dict[str, int], key: str, catalog: ModelCatalog | None = None) -> float:
    """1回の呼び出しの費用（米ドル）。"""
    m = spec(key, catalog)
    return (
        usage.get("input_tokens", 0) * m.input
        + usage.get("output_tokens", 0) * m.output
        + usage.get("cache_read_input_tokens", 0) * m.cache_read
        + usage.get("cache_creation_input_tokens", 0) * m.cache_write
    ) / 1_000_000


@dataclass
class ModelChoice:
    """どのモデルを使うか。既定、役割ごとの上書き、断られたときに次に試す順番。"""

    default: str = DEFAULT_MODEL
    per_role: dict[str, str] = field(default_factory=dict)
    fallback_chain: list[str] = field(default_factory=list)

    def model_for(self, role: str) -> str:
        return normalize(self.per_role.get(role) or self.default)

    def attempts(self, role: str) -> list[str]:
        """この役割で試す順番（重複は除く）。"""
        order = [self.model_for(role)] + [normalize(k) for k in self.fallback_chain]
        return list(dict.fromkeys(order))


class ModelSettings:
    """モデルの選択を ``state/ai_settings.json`` に保存する（画面から切り替えられるように）。

    環境変数 ``AI_MODEL`` があれば、保存した既定より優先する（Docker などで固定したいとき）。
    """

    DEFAULT_PATH = STATE / "ai_settings.json"

    def __init__(self, path: Path | str = DEFAULT_PATH, environ: dict[str, str] | None = None) -> None:
        self.path = Path(path)
        self._environ = environ

    def load(self) -> ModelChoice:
        choice = ModelChoice()
        if self.path.exists():
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            choice = ModelChoice(
                default=normalize(raw.get("default") or DEFAULT_MODEL),
                per_role={r: normalize(m) for r, m in dict(raw.get("per_role") or {}).items() if m},
                fallback_chain=[normalize(k) for k in raw.get("fallback_chain") or [] if k],
            )
        environ = os.environ if self._environ is None else self._environ
        if environ.get("AI_MODEL"):
            choice.default = normalize(environ["AI_MODEL"])
        choice.per_role = {r: m for r, m in choice.per_role.items() if r in ROLES}
        return choice

    def save(self, choice: ModelChoice) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(choice), ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
