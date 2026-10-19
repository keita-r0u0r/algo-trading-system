"""AI パーツの中だけで使う口。本体の ports.py には置かない（本体は AI を知らない）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelReply:
    """言語モデルの返答。``data`` は構造化出力（JSON）を求めたときだけ入る。"""

    text: str
    data: dict[str, Any] | None
    model: str
    #: input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens
    usage: dict[str, int]


class LanguageModel(Protocol):
    """言語モデルの口。実装は ``claude.py``。テストでは偽物に差し替える。"""

    def ask(
        self, *, system: str, prompt: str, schema: dict[str, Any] | None = None, effort: str = "medium"
    ) -> ModelReply: ...


class UsageLog(Protocol):
    """AI の利用記録（追記のみ）。費用の上限の判定に使う。"""

    def append(self, record: dict[str, Any]) -> None: ...
    def spent_since(self, start: str) -> float: ...
