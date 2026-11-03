"""経済指標の予定（今週分）を取ってきて、``state/fx_calendar.json`` に置く。

出どころは Forex Factory の週間カレンダー（JSON。キー不要）
https://nfs.faireconomy.media/ff_calendar_thisweek.json 。時刻・通貨・指標名・重要度・予想・前回が入っている。
頻繁に取りに行くと断られるので、``max_age``（既定1時間）より新しければ置いてあるものを使う。取れなかったときも、
置いてあるもの（古くても）を使い、売買の判断は止めない。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def read(path: Path) -> dict[str, Any]:
    """置いてある予定（{fetched_at, events, error}）。無ければ空。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load(path: Path, now: dt.datetime, *, max_age: dt.timedelta = dt.timedelta(hours=1)) -> dict[str, Any]:
    """予定を返す。古ければ取り直して ``path`` に書く。"""
    cached = read(path)
    fetched = cached.get("fetched_at")
    if fetched and now - dt.datetime.fromisoformat(fetched) < max_age:
        return cached
    try:
        r = httpx.get(URL, timeout=20, headers={"User-Agent": "algo-trading-system"})
        r.raise_for_status()
        data = {"fetched_at": now.isoformat(timespec="seconds"), "events": r.json(), "error": ""}
    except Exception as exc:  # noqa: BLE001  取れなくても判断は止めない（前に取った分を使う）
        data = {**cached, "error": f"{now:%m-%d %H:%M} {type(exc).__name__}: {str(exc)[:160]}"}
        data.setdefault("events", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data
