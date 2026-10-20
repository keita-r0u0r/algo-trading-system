"""Claude の呼び出し（Anthropic 直接 / Vertex AI）。``LanguageModel`` の実装。

  * 呼び出し先は、作るときに渡すクライアントで決まる（``anthropic.Anthropic`` / ``anthropic.AnthropicVertex``）
  * 構造化出力（``output_config.format``）で、決まった形の JSON を受け取る。考える深さ（effort）を明示する
  * 共通の前提（system）にキャッシュの印を付ける
  * 断られた（refusal）ときは ``RefusalError``。Anthropic 直接の対応モデルでは、その前にサーバー側で
    推奨の別モデルに切り替える（``fallbacks: "default"``）。Vertex AI にはこの仕組みが無い
  * ``anthropic`` は使うときに読み込む（未導入でも、本体と AI パーツの読み込みは壊れない）
"""

from __future__ import annotations

import json
from typing import Any

from .assistant import AiError, RefusalError
from .ports import ModelReply

FALLBACK_BETA = "server-side-fallback-2026-07-01"
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def anthropic_client(api_key: str) -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise AiError('anthropic のライブラリがありません。pip install -e ".[ai]" を実行してください') from exc
    return anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=2)


def vertex_client(project: str, region: str) -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise AiError('anthropic のライブラリがありません。pip install -e ".[ai]" を実行してください') from exc
    return anthropic.AnthropicVertex(project_id=project, region=region, timeout=180.0, max_retries=2)


class ClaudeModel:
    def __init__(self, api_key: str = "", *, client: Any = None, server_fallback: bool = True) -> None:
        self._client = client if client is not None else anthropic_client(api_key)
        #: サーバー側の切り替えを使うか（Anthropic 直接のときだけ。対応モデルかは呼ぶ側が決める）
        self.server_fallback = server_fallback

    def __repr__(self) -> str:  # APIキーを出さない
        return "ClaudeModel()"

    def ask(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        effort: str = "medium",
        server_fallback: bool | None = None,
    ) -> ModelReply:
        output_config: dict[str, Any] = {"effort": effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": 16000,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        use_fallback = self.server_fallback if server_fallback is None else server_fallback
        try:
            if use_fallback:
                response = self._client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                response = self._client.messages.create(**request)
        except Exception as exc:
            raise AiError(_describe(exc)) from None

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise RefusalError(f"AI が答えを控えました（分類: {category or '不明'}）")
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
        return ModelReply(text=text, data=data, model=model, usage=usage,
                          served_model=getattr(response, "model", model) or model)


def _describe(exc: Exception) -> str:
    """API の例外を、利用者向けの説明にする。キーなどの値は含めない。"""
    name = type(exc).__name__
    messages = {
        "AuthenticationError": "認証に失敗しました（APIキー、または gcloud のログインを確認）",
        "PermissionDeniedError": "権限がありません（Vertex AI では、Model Garden でこのモデルを有効にしているかを確認）",
        "RateLimitError": "利用の上限に達しました。しばらく待ってから試してください",
        "APITimeoutError": "応答が時間内に返りませんでした。しばらく待ってから試してください",
        "APIConnectionError": "API に接続できません。ネットワークを確認してください",
        "BadRequestError": "リクエストが受け付けられませんでした（設定やモデル名を確認）",
        "NotFoundError": "モデルが見つかりません（モデルID・リージョン・有効化を確認）",
    }
    if name in messages:
        return messages[name]
    status = getattr(exc, "status_code", None)
    if status and status >= 500:
        return f"サービス側の一時的なエラーです（{status}）。しばらく待ってから試してください"
    return f"AI を呼べませんでした（{name}）"
