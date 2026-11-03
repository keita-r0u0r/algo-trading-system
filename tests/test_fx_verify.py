"""反映後の確認（scripts/fx_verify.py）: 起動後の判断を、記録した時刻で数える。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fx_verify  # noqa: E402


def test_decision_rounded_to_minute_still_counts_after_start():
    # 判断の時刻は 12:19:00（分で切り捨て）、起動は 12:19:09、記録は 03:19:55Z（= 12:19:55）
    e = {"kind": "live_decision", "account": "ai", "at": "2026-11-03T12:19:00+09:00",
         "logged_at": "2026-11-03T03:19:55+00:00", "model": "gemini"}
    assert fx_verify.after([e], "live_decision", "ai", "2026-11-03T12:19:09+09:00") == [e]
    old = {**e, "logged_at": "2026-11-03T03:18:00+00:00"}
    assert fx_verify.after([old], "live_decision", "ai", "2026-11-03T12:19:09+09:00") == []
