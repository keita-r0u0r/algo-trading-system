"""運用の操作盤（GUI）を起動する。ブラウザで http://127.0.0.1:8501 を開く。

  python scripts/gui.py

このMacからだけ接続できる（``.streamlit/config.toml``）。注文は出さない。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "jqbt" / "interfaces" / "gui" / "app.py"

if __name__ == "__main__":
    raise SystemExit(
        subprocess.call([sys.executable, "-m", "streamlit", "run", str(APP), *sys.argv[1:]], cwd=ROOT)
    )
