"""Jev（TypeSafe の System One モデル）を呼ぶ。キーは Secret Manager の ``typesafe-api-key``。

API は https://docs.typesafe.ai/api/system-one.md 。state（材料）と、型の決まった questions を送ると、
質問ごとに型どおりの答え（noul = はいの確からしさ、score = 段階、choice = 選択肢）と確信度が返る。
自由な文は返さない。費用は入力100万トークンあたり $0.042（出力は無料）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from .secrets import SecretError, SecretName, build_secrets, register_secret

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

register_secret(SecretName("typesafe_api_key", "TYPESAFE_API_KEY", "typesafe-api-key", "TypeSafe（Jev）の APIキー"))


class JevClient:
    def __init__(self, key: str, *, model: str = MODEL, timeout: float = 10.0) -> None:
        self._key, self.model, self.timeout = key, model, timeout

    def ask(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        r = httpx.post(URL, timeout=self.timeout,
                       headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                       json={"model": self.model, "state": state, "questions": questions})
        if r.status_code >= 400:
            raise RuntimeError(f"Jev が {r.status_code} を返した: {r.text[:200]}")
        return r.json()

    def __repr__(self) -> str:
        return f"JevClient(model={self.model!r})"


def jev_from_secrets() -> JevClient | None:
    """キーが置いてあれば Jev の呼び出し口。無ければ None。"""
    try:
        key = build_secrets().get("typesafe_api_key")
    except SecretError:
        return None
    return JevClient(key) if key else None


def log_usage(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
