"""反映後の確認（scripts/fx_verify.py）: 起動後の判断を、記録した時刻で数える。"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fx_verify  # noqa: E402

from jqbt.application.fx_live_trade import LiveConfig  # noqa: E402


def test_decision_rounded_to_minute_still_counts_after_start():
    # 判断の時刻は 12:19:00（分で切り捨て）、起動は 12:19:09、記録は 03:19:55Z（= 12:19:55）
    e = {"kind": "live_decision", "account": "ai", "at": "2026-11-03T12:19:00+09:00",
         "logged_at": "2026-11-03T03:19:55+00:00", "model": "gemini"}
    assert fx_verify.after([e], "live_decision", "ai", "2026-11-03T12:19:09+09:00") == [e]
    old = {**e, "logged_at": "2026-11-03T03:18:00+00:00"}
    assert fx_verify.after([old], "live_decision", "ai", "2026-11-03T12:19:09+09:00") == []


class Bucket:
    def __init__(self, files):
        self.files = files

    def read(self, name):
        return self.files.get(name, b"")

    def exists(self, name):
        return name in self.files

    def write(self, name, data):
        self.files[name] = data


NOW = dt.datetime(2026, 11, 9, 8, 0, tzinfo=fx_verify.JST)
ALL_LIVE_BUT_AI = {name: "live" for name in LiveConfig().accounts} | {"ai": "off"}


def bucket(version="v2", stages=None, events=()):
    return Bucket({
        "state/fx_watch.json": json.dumps({"version": version, "started_at": "2026-11-09T07:00:00+09:00",
                                           "last_ok_cycle": "2026-11-09T07:55:00+09:00",
                                           "last_tick": "2026-11-09T07:59:50+09:00"}).encode(),
        "state/fx_settings.json": json.dumps({"deciders": stages or ALL_LIVE_BUT_AI}).encode(),
        "state/fx_live.jsonl": "".join(json.dumps(e) + "\n" for e in events).encode(),
    })


def decisions(accounts):
    return [{"kind": "live_decision", "account": a, "at": "2026-11-09T07:55:00+09:00",
             "logged_at": "2026-11-09T07:55:10+09:00", "logic": {"rule": "x"}} for a in accounts]


def test_waits_for_watcher_then_times_out_when_it_never_swaps(monkeypatch):
    monkeypatch.setattr(fx_verify, "check_all", lambda *a, **k: [])
    b = bucket(version="v1")
    assert fx_verify.auto_verify(b.read, b.exists, b.write, "v2", NOW) == "wait"
    assert "state/releases.jsonl" not in b.files
    later = NOW + fx_verify.AUTO_VERIFY_LIMIT
    assert fx_verify.auto_verify(b.read, b.exists, b.write, "v2", later) == "wait"
    row = json.loads(b.files["state/releases.jsonl"])
    assert row["version"] == "v2" and row["result"] == "wait"
    assert fx_verify.auto_verify(b.read, b.exists, b.write, "v2", later) is None  # 残したら、もう見ない


def test_records_once_and_does_not_require_ai_when_ai_is_off(monkeypatch):
    monkeypatch.setattr(fx_verify, "check_all", lambda *a, **k: [])
    b = bucket(events=decisions(["jev"]))
    assert fx_verify.auto_verify(b.read, b.exists, b.write, "v2", NOW) == "ok"
    assert json.loads(b.files["state/releases.jsonl"])["result"] == "ok"
    assert fx_verify.auto_verify(b.read, b.exists, b.write, "v2", NOW) is None


def test_ai_is_required_when_live(monkeypatch):
    monkeypatch.setattr(fx_verify, "check_all", lambda *a, **k: [])
    b = bucket(stages={"ai": "live"}, events=decisions(["jev"]))
    assert fx_verify.check_once(b.read, b.exists, "v2")[0] == "wait"


def test_not_live_deciders_are_not_health_checked(monkeypatch):
    seen = {}
    monkeypatch.setattr(fx_verify, "check_all", lambda *a, **k: seen.update(k) or [])
    b = bucket(events=decisions(["jev"]))
    fx_verify.check_once(b.read, b.exists, "v2")
    assert seen["not_live"] == ["ai"]


def test_changelog_lists_only_prs_after_the_last_recorded():
    text = "## #70 · 2026-11-07 · 新しい\n本文\n## #65 · 2026-11-07 · 前の\n"
    assert fx_verify.changelog_prs([{"prs": [65]}], text) == ([70], {"70": "新しい"})


def test_merged_prs_without_git_returns_nothing(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr("subprocess.run", missing)
    assert fx_verify.merged_prs(None, "v2") == ([], {})
