"""AI の利用記録（追記のみ）。``state/ai_usage.jsonl``（gitignore 対象）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[4] / "state" / "ai_usage.jsonl"


class JsonlUsageLog:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def spent_since(self, start: str) -> float:
        """``start``（YYYY-MM-DD）以降の費用の合計（米ドル）。"""
        return float(sum(
            r.get("cost_usd", 0.0) for r in self.records()
            if r.get("kind", "call") == "call" and r.get("at", "")[:10] >= start
        ))
