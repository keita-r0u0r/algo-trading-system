"""FX の実際の約定の記録（追記のみ、1行1件の JSON）。``state/fx_live.jsonl``。

約定（execution）、建玉と資産の状態（snapshot）、読めなかったこと（error）を、起きた順に足す。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(os.environ.get("JQBT_FX_LIVE_LOG") or Path(__file__).resolve().parents[3] / "state" / "fx_live.jsonl")


def append(events: list[dict[str, Any]], path: Path = DEFAULT_PATH) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps({"logged_at": now, **event}, ensure_ascii=False, default=str) + "\n")


def load(path: Path = DEFAULT_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def known_execution_ids(events: list[dict[str, Any]]) -> set[str]:
    return {str(e["executionId"]) for e in events if e.get("kind") == "execution" and "executionId" in e}
