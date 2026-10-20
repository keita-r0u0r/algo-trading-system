"""AI の拡張パーツ（Claude / Gemini。Vertex AI または Anthropic 直接）。

有効にする: ``JQBT_EXTENSIONS=ai``（.env）、``pip install -e ".[ai]"``。
既定は Vertex AI（``GCP_PROJECT`` と gcloud のログイン）。Anthropic を直接使うときだけ、
APIキーを GCP Secret Manager の ``anthropic-api-key`` に置く（docs/ai.md）。

本体はこのパーツを import しない。``interfaces/extensions.py`` が名前で読み込み、
``extension()`` が返すものを通じて、GUI の画面を足す。
"""

from __future__ import annotations

import os
from typing import Any

from ...infrastructure.secrets import SecretName, register_secret
from ...interfaces.extensions import GuiPage
from .assistant import AiAssistant, AiBudget, AiError

SECRET = SecretName("anthropic_api_key", "ANTHROPIC_API_KEY", "anthropic-api-key", "Anthropic（Claude）の APIキー")
register_secret(SECRET)


def budget_from_env(environ: Any = None) -> AiBudget:
    environ = os.environ if environ is None else environ
    try:
        return AiBudget(monthly_usd=float(environ.get("AI_MONTHLY_BUDGET_USD", "5")))
    except ValueError:
        return AiBudget()


def build_assistant(secrets: Any = None, model: Any = None, usage: Any = None, settings: Any = None) -> AiAssistant:
    """本物の部品をつないで、アシスタントを作る。テストでは model・usage・settings を差し替える。

    model を渡さなければ、呼び出し先（Vertex AI / Anthropic 直接）に振り分けるルーターを使う。
    """
    from .models import ModelSettings
    from .providers import ProviderRouter
    from .usage import JsonlUsageLog

    return AiAssistant(
        model or ProviderRouter(secrets=secrets),
        usage or JsonlUsageLog(),
        budget_from_env(),
        settings or ModelSettings(),
    )


class AiExtension:
    name = "ai"
    title = "AI"
    description = "検証の解説、運用の見張り、売買ルールの案、ペーパー運用での売買判断"

    def available(self) -> tuple[bool, str]:
        """既定のモデルを呼ぶ準備ができているか（API は呼ばない）。"""
        from .models import ModelSettings
        from .providers import ProviderRouter

        default = ModelSettings().load().default
        ok, reason = ProviderRouter().readiness(default)
        return ok, "" if ok else f"{default}: {reason}"

    def gui_pages(self) -> list[GuiPage]:
        from .gui import render

        return [GuiPage(self.title, render)]


def extension() -> AiExtension:
    return AiExtension()


__all__ = ["AiExtension", "AiError", "build_assistant", "extension"]
