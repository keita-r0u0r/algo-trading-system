"""Gemini（Vertex AI）の呼び出し。``LanguageModel`` の実装。``google-genai`` を使う。

  * ``genai.Client(enterprise=True, project=..., location=...)`` で Vertex AI に接続する（認証は gcloud のログイン）
  * 決まった形の答え: ``response_mime_type="application/json"`` と ``response_json_schema``
  * 考える深さ: 3.x 以降は ``thinking_level``（LOW / MEDIUM / HIGH）、2.x は ``thinking_budget``（トークン数）。
    2.x に thinking_level を送ると 400 になる（2026-10-20 に Gemini 2.5 Flash で確認）
  * 使ったトークン: 思考（``thoughts_token_count``）も出力として課金されるので、出力に足す
  * 安全上の理由で止まった（``prompt_feedback.block_reason`` / ``finish_reason`` が SAFETY など）ときは ``RefusalError``
"""

from __future__ import annotations

import json
from typing import Any

from .assistant import AiError, RefusalError
from .ports import ModelReply

LEVELS = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}
#: 2.x の思考に使うトークン数の上限（effort に対応させる）
BUDGETS = {"low": 1024, "medium": 4096, "high": 8192}


def thinking_config(model: str, effort: str) -> dict[str, Any]:
    """モデルの世代に合わせた、考える深さの指定。"""
    if model.startswith("gemini-2."):
        return {"thinking_budget": BUDGETS.get(effort, BUDGETS["medium"])}
    return {"thinking_level": LEVELS.get(effort, "MEDIUM")}
#: 安全上の理由で止まった扱いにする finish_reason
BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def gemini_client(project: str, location: str) -> Any:
    try:
        from google import genai
    except ImportError as exc:
        raise AiError('google-genai のライブラリがありません。pip install -e ".[ai]" を実行してください') from exc
    return genai.Client(enterprise=True, project=project, location=location)


def _name(value: Any) -> str:
    return getattr(value, "name", None) or str(value or "")


class GeminiModel:
    def __init__(self, project: str = "", location: str = "global", *, client: Any = None) -> None:
        self._client = client if client is not None else gemini_client(project, location)

    def __repr__(self) -> str:
        return "GeminiModel()"

    def ask(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any] | None = None, effort: str = "medium"
    ) -> ModelReply:
        config: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": 16000,
            "thinking_config": thinking_config(model, effort),
        }
        if schema is not None:
            config.update(response_mime_type="application/json", response_json_schema=schema)
        try:
            response = self._client.models.generate_content(model=model, contents=prompt, config=config)
        except Exception as exc:
            raise AiError(_describe(exc)) from None

        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise RefusalError(f"AI が答えを控えました（分類: {_name(feedback.block_reason)}）")
        candidates = getattr(response, "candidates", None) or []
        reason = _name(getattr(candidates[0], "finish_reason", None)) if candidates else ""
        if reason in BLOCKED:
            raise RefusalError(f"AI が答えを控えました（分類: {reason}）")
        if reason == "MAX_TOKENS":
            raise AiError("答えが長すぎて途中で切れました。質問を絞ってください")

        text = getattr(response, "text", None) or ""
        data = None
        if schema is not None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
        meta = getattr(response, "usage_metadata", None)
        count = lambda name: int(getattr(meta, name, 0) or 0) if meta is not None else 0  # noqa: E731
        cached = count("cached_content_token_count")
        usage = {
            "input_tokens": max(count("prompt_token_count") - cached, 0),
            "output_tokens": count("candidates_token_count") + count("thoughts_token_count"),
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0,
        }
        return ModelReply(text=text, data=data, model=model, usage=usage, served_model=model)


def _describe(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    messages = {
        400: "リクエストが受け付けられませんでした（このモデルが対応していない設定の可能性があります）",
        401: "認証に失敗しました（gcloud auth application-default login を確認）",
        403: "権限がありません（Vertex AI の API の有効化と、プロジェクトの権限を確認）",
        404: "モデルが見つかりません（モデルID・リージョン・Model Garden での有効化を確認）",
        429: "利用の上限に達しました。しばらく待ってから試してください",
    }
    if code in messages:
        return messages[code]
    if isinstance(code, int) and code >= 500:
        return f"サービス側の一時的なエラーです（{code}）。しばらく待ってから試してください"
    return f"AI を呼べませんでした（{type(exc).__name__}）"
