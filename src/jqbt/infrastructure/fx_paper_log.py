"""FX のペーパー運用の記録（追記のみ、1行1件の JSON）。

``state/fx_paper.jsonl``。判断・約定・手仕舞い・口座の状態（snapshot）を、起きた順に足していく。
口座の今の状態は、口座ごとの最後の snapshot。書くのは1時間に1回の実行だけなので、ファイル1つで足りる
（Cloud Run では、Cloud Storage を直接つないだ state/ に置き、画面からも読む）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "state" / "fx_paper.jsonl"


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


def latest_snapshots(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """口座ごとの最後の状態。"""
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") == "snapshot":
            out[event["account"]] = event["state"]
    return out
