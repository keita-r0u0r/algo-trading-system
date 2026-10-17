"""実験の記録（追記のみ）。

「何回試したか」は、多重検定の補正（``core/selection.py``）の入力になる。
後から消せたり書き換えられたりすると、試行回数が嘘になる。
だから **追記だけ** にして、読むときも全行を数える。

保存先は ``state/experiments.jsonl``（gitignore 対象）。1行1件の JSON。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "state" / "experiments.jsonl"


def append(record: dict[str, Any], path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **record}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


def load(path: Path = DEFAULT_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def lockbox_used(data_end: str, path: Path = DEFAULT_PATH) -> bool:
    """同じデータ末尾に対して、ロックボックスを既に評価したか。

    一度見た検証用期間は、もう「未知」ではない。見たあとに戦略を直して
    また見る、を繰り返すと、検証用期間への合わせ込みになる。
    """
    return any(
        r.get("kind") == "lockbox" and r.get("data_end") == data_end for r in load(path)
    )
