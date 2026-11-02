"""段取りの部品（interfaces/fx_runtime.py）: 回の重なり防止・見張り役への受け渡し・AI を呼ぶ間隔。"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from jqbt.interfaces import fx_runtime as rt

JST = dt.timezone(dt.timedelta(hours=9))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "STARTED", tmp_path / "fx_started")
    monkeypatch.setattr(rt, "WATCH_STATE", tmp_path / "fx_watch.json")
    monkeypatch.setattr(rt, "AI_LAST", tmp_path / "fx_ai_last")
    return tmp_path


def ago(seconds: float) -> str:
    return (dt.datetime.now(JST) - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_claim_run_blocks_overlap(state):
    assert rt.claim_run()
    assert not rt.claim_run()  # 直前に始まった回があれば何もしない
    (state / "fx_started").write_text(ago(rt.OVERLAP_SECONDS + 5))
    assert rt.claim_run()


def test_claim_run_ignores_broken_marker(state):
    (state / "fx_started").write_text("こわれた")
    assert rt.claim_run()


@pytest.mark.parametrize(("data", "expected"), [
    ({"trading": True, "last_cycle": ago(30)}, True),
    ({"trading": True, "last_cycle": ago(rt.WATCH_FRESH_SECONDS + 60)}, False),  # 止まっている → ジョブが代わる
    ({"trading": False, "last_cycle": ago(30)}, False),  # 見張るだけ
    ({"trading": True, "last_cycle": None}, False),
])
def test_watcher_trades(state, data, expected):
    (state / "fx_watch.json").write_text(json.dumps(data))
    assert rt.watcher_trades() is expected


def test_watcher_trades_without_file(state):
    assert rt.watcher_trades() is False


def test_ai_due_interval_and_trigger(state):
    assert rt.ai_due("")  # 初回は呼ぶ
    assert not rt.ai_due("")  # 15分たつまでは呼ばない
    assert rt.ai_due("USD_JPY が急変")  # 起こされた回は必ず呼ぶ
    (state / "fx_ai_last").write_text(ago(rt.AI_INTERVAL_SECONDS))
    assert rt.ai_due("")
