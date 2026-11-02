"""GMOコインの実際の約定・建玉・資産を読み、記録する。**読むだけ。注文は出さない。**

  python scripts/fx_live_sync.py

約定の履歴は API の都合で直近1日分しか取れないので、1日1回以上動かす（Cloud Run ではペーパー運用と一緒に15分ごと）。
記録は ``state/fx_live.jsonl``。APIキーに照会の権限が足りないものは、読めなかったことを記録して進める。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jqbt.interfaces.fx_live import sync  # noqa: E402

if __name__ == "__main__":
    sync()
