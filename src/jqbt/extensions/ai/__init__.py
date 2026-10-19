"""AI（Claude）の拡張パーツ。

有効にする: ``JQBT_EXTENSIONS=ai``（.env）、``pip install -e ".[ai]"``、
Anthropic の APIキーを GCP Secret Manager の ``anthropic-api-key`` に置く（docs/ai.md）。

本体はこのパーツを import しない。``interfaces/extensions.py`` が名前で読み込み、
``extension()`` が返すものを通じて、GUI の画面を足す。
"""

from __future__ import annotations

import os
from typing import Any

from ...infrastructure.secrets import SecretError, SecretName, build_secrets, register_secret, require
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


def build_assistant(secrets: Any = None, model: Any = None, usage: Any = None) -> AiAssistant:
    """本物の部品をつないで、アシスタントを作る。テストでは model と usage を差し替える。"""
    from .usage import JsonlUsageLog

    if model is None:
        from .claude import ClaudeModel

        model = ClaudeModel(api_key=require(secrets or build_secrets(), SECRET.key))
    return AiAssistant(model, usage or JsonlUsageLog(), budget_from_env())


class AiExtension:
    name = "ai"
    title = "AI（Claude）"
    description = "検証の解説、運用の見張り、売買ルールの案、ペーパー運用での売買判断"

    def available(self) -> tuple[bool, str]:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, 'anthropic のライブラリがありません（pip install -e ".[ai]"）'
        try:
            if not build_secrets().get(SECRET.key):
                return False, f"{SECRET.description} がありません（docs/ai.md）"
        except SecretError as exc:
            return False, str(exc)
        return True, ""

    def gui_pages(self) -> list[GuiPage]:
        from .gui import render

        return [GuiPage(self.title, render)]


def extension() -> AiExtension:
    return AiExtension()


__all__ = ["AiExtension", "AiError", "build_assistant", "extension"]
