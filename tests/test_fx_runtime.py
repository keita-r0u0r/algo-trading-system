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
    ({"trading": True, "last_ok_cycle": ago(30)}, True),
    ({"trading": True, "last_ok_cycle": ago(rt.WATCH_FRESH_SECONDS + 60)}, False),  # 止まっている・口座に届かない → ジョブが代わる
    ({"trading": False, "last_ok_cycle": ago(30)}, False),  # 見張るだけ
    ({"trading": True, "last_ok_cycle": None}, False),
    ({"trading": True, "last_cycle": ago(30), "started_at": ago(3600)}, False),  # 回は回っているが口座に届いていない
    ({"trading": True, "last_ok_cycle": None, "started_at": ago(60)}, True),  # 起動直後: 最初の回までは担当
    ({"trading": False, "started_at": ago(60)}, False),
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


@pytest.mark.parametrize(("last", "expected"), [(100.0, rt.CARRY_MIN_SCALE), (101.0, 0.5), (103.0, 1.0), (110.0, 1.0)])
def test_carry_scale_grows_with_trend(last, expected):
    closes = [100.0] * 49 + [last]
    average = sum(closes) / 50
    assert rt.carry_scale(closes) == pytest.approx(min(1.0, max(rt.CARRY_MIN_SCALE, (last / average - 1) / 0.02)))
    assert rt.carry_scale(closes) == pytest.approx(expected, abs=0.02)


def test_position_numbers_are_precomputed_for_ai():
    from jqbt.application.fx_paper import PaperPosition

    long = PaperPosition("USD_JPY", 100, 157.876, 157.676, None, "2026-11-03T00:55:00+09:00", "")
    n = rt.position_numbers(long, {"bid": 157.918, "ask": 157.922})
    assert n["unrealized_pips"] == 4.2 and n["unrealized_yen"] == 4.2  # 100通貨なら 1 pip = 1円
    assert n["pips_to_stop"] == 24.2 and n["yen_per_pip"] == 1.0
    short = PaperPosition("EUR_JPY", -1000, 176.737, 177.0, 176.5, "", "")
    s = rt.position_numbers(short, {"bid": 176.70, "ask": 176.717})
    assert s["unrealized_pips"] == 2.0 and s["unrealized_yen"] == 20.0 and s["pips_to_take"] == 21.7


def test_ai_skip_reason_says_when_and_why(state):
    now = dt.datetime(2026, 11, 3, 12, 5, tzinfo=JST)
    ctx = rt.Context(api=None, now=now, frame=None, carry_frame=None, live_frame=None, insts={}, quotes={}, bars={},
                     ai=object(), today_rates={}, short={}, ai_due=False, ai_last=now - dt.timedelta(minutes=5))
    text = rt.ai_skip_reason(ctx)
    assert "12:00（5分前）" in text and "12:15 ごろ" in text and "急変" in text
    [(decision, _, extra)] = rt.decide("ai", None, ctx, [])
    assert decision == {"action": "hold"} and extra["note"] == text


def test_plan_when_opened_is_found_for_paper_and_live():
    from jqbt.application.fx_paper import PaperPosition

    now = dt.datetime(2026, 11, 3, 13, 0, tzinfo=JST)
    decision = {"action": "open_long", "symbol": "USD_JPY", "reasoning": "金利差と1時間足の上抜け",
                "exit_condition": "1時間足の終値が 157.80 を下回ったら", "hold_minutes": 120, "stop_pips": 10}
    live = [{"kind": "live_decision", "account": "ai", "at": "2026-11-03T12:40:00+09:00", "approved": True,
             "decision": decision},
            {"kind": "live_decision", "account": "ai", "at": "2026-11-03T12:45:00+09:00", "approved": False,
             "decision": {"action": "hold"}}]
    position = PaperPosition("USD_JPY", 991, 157.933, 157.833, 158.133, "2026-11-03T12:40:54+09:00", "")
    plan = rt.plan_when_opened("ai", position, live, now)
    assert plan["exit_condition"].startswith("1時間足") and plan["hold_minutes"] == 120 and plan["minutes_held"] == 19
    # 建玉より後の判断や、別の通貨ペア・通らなかった判断は使わない
    later = [{**live[0], "at": "2026-11-03T12:50:00+09:00"}]
    assert rt.plan_when_opened("ai", position, later, now) is None
    paper = [{"kind": "decision", "account": "ai", "at": "2026-11-03T12:40:00+09:00", "approved": True,
              "decision": {**decision, "symbol": "EUR_JPY"}}]
    assert rt.plan_when_opened("ai", position, paper, now) is None
    assert rt.plan_when_opened("ai", None, live, now) is None
