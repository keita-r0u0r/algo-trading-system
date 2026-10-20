"""呼び出し先への振り分け。``LanguageModel`` を満たし、キー（呼び出し先:モデルID）で部品を選ぶ。

| 呼び出し先 | 種類 | 部品 | 必要なもの |
|---|---|---|---|
| vertex    | claude | ClaudeModel（AnthropicVertex） | GCP_PROJECT、gcloud のログイン、Model Garden での有効化 |
| vertex    | gemini | GeminiModel（google-genai）    | 同上 |
| anthropic | claude | ClaudeModel（Anthropic）       | Anthropic の APIキー |

部品は最初に使うときに作る（使わない呼び出し先の準備は要らない）。
新しい呼び出し先（例: ローカルの Ollama）は ``models.PROVIDERS`` と ``_build`` に足す。
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import replace
from typing import Any, Callable, Mapping

from .assistant import AiError
from .models import PROVIDERS, ModelCatalog, spec, split
from .ports import ModelReply

DEFAULT_REGION = "global"


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class ProviderRouter:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        secrets: Any = None,
        catalog: ModelCatalog | None = None,
        factories: dict[tuple[str, str], Callable[[], Any]] | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._secrets = secrets
        self.catalog = catalog or ModelCatalog()
        #: テストでは部品の作り方を差し替える
        self._factories = factories or {}
        self._adapters: dict[tuple[str, str], Any] = {}

    @property
    def project(self) -> str:
        return self._environ.get("GCP_PROJECT", "")

    @property
    def region(self) -> str:
        return self._environ.get("AI_VERTEX_REGION", "") or DEFAULT_REGION

    # ------------------------------------------------------------------
    def readiness(self, key: str) -> tuple[bool, str]:
        """そのモデルを呼ぶ準備ができているか。できていなければ理由を返す（API は呼ばない）。"""
        m = spec(key, self.catalog)
        if m.provider not in PROVIDERS:
            return False, f"未知の呼び出し先です（{m.provider}）"
        if m.provider == "vertex":
            if not self.project:
                return False, "GCP_PROJECT が設定されていません（Vertex AI を使うには必須）"
            module = "google.genai" if m.family == "gemini" else "anthropic"
            if not _installed(module):
                return False, f'{module} のライブラリがありません（pip install -e ".[ai]"）'
            return True, ""
        if m.family != "claude":
            return False, "Anthropic 直接で呼べるのは Claude だけです"
        if not _installed("anthropic"):
            return False, 'anthropic のライブラリがありません（pip install -e ".[ai]"）'
        from ...infrastructure.secrets import SecretError, build_secrets

        try:
            if not (self._secrets or build_secrets()).get("anthropic_api_key"):
                return False, "Anthropic の APIキーがありません（docs/ai.md）"
        except SecretError as exc:
            return False, str(exc)
        return True, ""

    def _build(self, provider: str, family: str) -> Any:
        if (provider, family) in self._factories:
            return self._factories[(provider, family)]()
        if provider == "vertex":
            if not self.project:
                raise AiError("GCP_PROJECT が設定されていません（Vertex AI を使うには必須）")
            if family == "gemini":
                from .gemini import GeminiModel

                return GeminiModel(self.project, self.region)
            from .claude import ClaudeModel, vertex_client

            return ClaudeModel(client=vertex_client(self.project, self.region), server_fallback=False)
        if provider == "anthropic" and family == "claude":
            from ...infrastructure.secrets import build_secrets, require
            from .claude import ClaudeModel

            return ClaudeModel(api_key=require(self._secrets or build_secrets(), "anthropic_api_key"))
        raise AiError(f"この組み合わせは呼べません（{provider} / {family}）")

    def ask(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any] | None = None, effort: str = "medium"
    ) -> ModelReply:
        m = spec(model, self.catalog)
        provider, _ = split(m.key)
        adapter = self._adapters.get((provider, m.family))
        if adapter is None:
            adapter = self._adapters[(provider, m.family)] = self._build(provider, m.family)
        kwargs: dict[str, Any] = dict(model=m.id, system=system, prompt=prompt, schema=schema, effort=effort)
        if m.family == "claude" and provider == "anthropic":
            kwargs["server_fallback"] = m.server_fallback
        reply = adapter.ask(**kwargs)
        return replace(reply, model=m.key)
