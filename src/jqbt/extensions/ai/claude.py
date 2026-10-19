"""Claude（Anthropic API）の呼び出し。``LanguageModel`` の実装。

  * モデルは Claude Opus 5.5。考える深さ（effort）は役割ごとに明示する（既定は medium）
  * 構造化出力（``output_config.format``）で、決まった形の JSON を受け取る
  * 安全上の理由で答えが控えられた（refusal）ときは、サーバー側で推奨の別モデルに切り替える
    （``fallbacks: "default"``）。それでも控えられたら、理由を添えて止める
  * 共通の前提（system）にキャッシュの印を付ける。毎回同じ文面なので、条件を満たせば安く読める
  * ``anthropic`` は呼ぶときに読み込む（未導入でも、本体と AI パーツの読み込みは壊れない）
"""

from __future__ import annotations

import json
from typing import Any

from .assistant import MODEL, AiError
from .ports import ModelReply

FALLBACK_BETA = "server-side-fallback-2026-07-01"
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


class ClaudeModel:
    def __init__(self, api_key: str, *, model: str = MODEL, client: Any = None, fallback: bool = True) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise AiError('anthropic のライブラリがありません。pip install -e ".[ai]" を実行してください') from exc
            client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=2)
        self._client = client
        self.model = model
        self.fallback = fallback

    def __repr__(self) -> str:  # APIキーを出さない
        return f"ClaudeModel(model={self.model!r})"

    def ask(
        self, *, system: str, prompt: str, schema: dict[str, Any] | None = None, effort: str = "medium"
    ) -> ModelReply:
        output_config: dict[str, Any] = {"effort": effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        if self.fallback:
            request.update(betas=[FALLBACK_BETA], fallbacks="default")

        try:
            response = self._client.beta.messages.create(**request)
        except Exception as exc:
            raise AiError(_describe(exc)) from None

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise AiError(f"AI が答えを控えました（分類: {category or '不明'}）。質問の書き方を変えて試してください")
        if response.stop_reason == "max_tokens":
            raise AiError("答えが長すぎて途中で切れました。質問を絞ってください")

        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        data = None
        if schema is not None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
        usage = {name: int(getattr(response.usage, name, 0) or 0) for name in USAGE_FIELDS}
        return ModelReply(text=text, data=data, model=getattr(response, "model", self.model), usage=usage)


def _describe(exc: Exception) -> str:
    """API の例外を、利用者向けの説明にする。キーなどの値は含めない。"""
    name = type(exc).__name__
    messages = {
        "AuthenticationError": "APIキーが正しくありません（docs/secrets.md の anthropic-api-key を確認）",
        "PermissionDeniedError": "APIキーに必要な権限がありません",
        "RateLimitError": "利用の上限に達しました。しばらく待ってから試してください",
        "APITimeoutError": "応答が時間内に返りませんでした。しばらく待ってから試してください",
        "APIConnectionError": "Anthropic の API に接続できません。ネットワークを確認してください",
        "BadRequestError": "リクエストが受け付けられませんでした（設定やモデル名を確認）",
        "NotFoundError": "モデルが見つかりません",
    }
    if name in messages:
        return messages[name]
    status = getattr(exc, "status_code", None)
    if status and status >= 500:
        return f"Anthropic 側の一時的なエラーです（{status}）。しばらく待ってから試してください"
    return f"AI を呼べませんでした（{name}）"
