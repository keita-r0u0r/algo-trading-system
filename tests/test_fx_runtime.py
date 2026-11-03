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


def test_active_notes_respects_expiry_and_delete(tmp_path):
    import datetime as dt
    import json

    from jqbt.interfaces.fx_runtime import active_notes

    jst = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime(2026, 11, 3, 22, 0, tzinfo=jst)
    path = tmp_path / "fx_notes.jsonl"
    rows = [{"kind": "note", "id": "a", "at": "2026-11-03T21:00:00+09:00", "until": "2026-11-04T00:00:00+09:00", "text": "建てない"},
            {"kind": "note", "id": "b", "at": "2026-11-03T18:00:00+09:00", "until": "2026-11-03T21:00:00+09:00", "text": "期限切れ"},
            {"kind": "note", "id": "c", "at": "2026-11-03T21:30:00+09:00", "until": "2026-11-04T00:00:00+09:00", "text": "消した"},
            {"kind": "delete", "id": "c"}]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    assert [n["text"] for n in active_notes(now, path)] == ["建てない"]


def test_ai_is_not_woken_for_rules_managed_positions(state):
    assert rt.ai_due("")
    assert not rt.ai_due("決まりの決済の線: harness の建玉が利益を守る線（158.1）を割った")
    assert rt.ai_due("見直し: ai の建玉の含み損が損切りまでの33%に達した")


def test_settings_round_trip(tmp_path):
    from jqbt.application.fx_filter import FilterSettings, FxSettings

    path = tmp_path / "fx_settings.json"
    assert rt.load_settings(path).enabled("harness")  # 無ければ既定
    rt.save_settings(FxSettings({"ai": False, "harness": True}, FilterSettings(mode="veto", provider="gemini")), path)
    loaded = rt.load_settings(path)
    assert not loaded.enabled("ai") and loaded.filter.mode == "veto" and loaded.filter.provider == "gemini"


def harness_ctx(features, quotes=None, bars=None, settings=None, jev=None):
    from jqbt.application.fx_filter import FxSettings
    from jqbt.core.fx.frame import TradingFrame
    from jqbt.core.fx.model import RISK_PROFILES

    now = dt.datetime(2026, 11, 4, 10, 0, tzinfo=JST)
    return rt.Context(api=None, now=now, frame=None, carry_frame=None, live_frame=TradingFrame(symbols=tuple(features), risk=RISK_PROFILES["live"]),
                      insts={}, quotes=quotes or {}, bars=bars or {s: None for s in features}, ai=None, today_rates={},
                      short={}, settings=settings or FxSettings(), jev=jev, features=dict(features))


def test_harness_opens_on_rules_and_asks_jev_only_to_skip(tmp_path, monkeypatch):
    from test_fx_setups import features

    from jqbt.application.fx_filter import FilterSettings, FxSettings
    from jqbt.application.fx_paper import PaperAccount

    monkeypatch.setattr(rt, "JEV_USAGE", tmp_path / "jev.jsonl")
    monkeypatch.setattr(rt, "NOTES", tmp_path / "notes.jsonl")

    class Jev:
        def __init__(self, skip):
            self.skip, self.asked = skip, []

        def ask(self, state, questions):
            self.asked.append((state, questions))
            return {"model": "jev-1", "answers": {"skip": {"noul": self.skip}, "quality": {"score": 2, "confidence": .5}},
                    "usage": {"input_tokens": 900}}

    empty = PaperAccount("harness", 10000, 10000, None, equity=10000, day_start_equity=10000)
    jev = Jev(0.9)
    d, reason, extra = rt.harness_decision(empty, harness_ctx({"USD_JPY": features()}, jev=jev), [])
    assert d["action"] == "open_long" and d["stop_pips"] == 16.0 and reason.startswith("決まり")
    assert d["exit_method"] == "chandelier" and extra["logic"]["exit_choice"]["by"] == "jev"  # 答えに無ければ決まった手法
    assert "記録だけ" in extra["note"] and extra["logic"]["filter"]["skip_probability"] == 0.9  # 既定は記録だけ
    assert jev.asked[0][0]["proposed_trade"]["symbol"] == "USD_JPY"
    assert (tmp_path / "jev.jsonl").exists()
    veto = FxSettings(filter=FilterSettings(mode="veto"))
    d, _, extra = rt.harness_decision(empty, harness_ctx({"USD_JPY": features()}, settings=veto, jev=Jev(0.9)), [])
    assert d["action"] == "hold" and "見送り" in extra["note"]
    d, _, _ = rt.harness_decision(empty, harness_ctx({"USD_JPY": features()}, settings=veto, jev=None), [])
    assert d["action"] == "open_long"  # キーが無ければ決まりのとおり


def test_harness_closes_by_its_exit_method():
    import pandas as pd
    from test_fx_setups import features

    from jqbt.application.fx_paper import PaperAccount, PaperPosition

    idx = pd.date_range("2026-11-04 09:00", periods=60, freq="1min")
    bars = pd.DataFrame({"high": [158.0] * 30 + [158.40] + [158.1] * 29, "low": [157.95] * 60}, index=idx)
    p = PaperPosition("USD_JPY", 300, 158.0, 157.84, None, "2026-11-04T09:00:00+09:00")
    account = PaperAccount("harness", 10000, 10000, p, equity=10000, day_start_equity=10000)
    ctx = harness_ctx({"USD_JPY": features()}, quotes={"USD_JPY": {"bid": 158.08, "ask": 158.10, "spread": 2}},
                      bars={"USD_JPY": bars})
    d, reason, extra = rt.harness_decision(account, ctx, [])  # シャンデリア: 158.40 − 0.30 = 158.10 を割った
    assert d["action"] == "close" and "158.1" in reason
    assert extra["logic"]["peak_pips"] == 40.0 and extra["logic"]["exit_level"] == 158.1
    events = [{"kind": "live_decision", "account": "harness", "approved": True, "at": "2026-11-04T08:59:00+09:00",
               "decision": {"action": "open_long", "symbol": "USD_JPY", "exit_method": "target"}}]
    d, _, extra = rt.harness_decision(account, ctx, events)  # 建てたときに選んだ手法（利確まで持つ）を使う
    assert d["action"] == "hold" and extra["logic"]["exit_method"] == "target"


def test_harness_lets_jev_pick_the_exit_method(tmp_path, monkeypatch):
    from test_fx_setups import features

    from jqbt.application.fx_filter import ExitSettings, FilterSettings, FxSettings
    from jqbt.application.fx_paper import PaperAccount

    monkeypatch.setattr(rt, "JEV_USAGE", tmp_path / "jev.jsonl")
    monkeypatch.setattr(rt, "NOTES", tmp_path / "notes.jsonl")

    class Jev:
        def ask(self, state, questions):
            assert set(questions) == {"exit_method"}  # 見送りは聞かない設定
            return {"answers": {"exit_method": {"choice": "target", "confidence": 0.8}}, "usage": {}}

    settings = FxSettings(filter=FilterSettings(mode="off"), exits=ExitSettings(selector="jev"))
    empty = PaperAccount("harness", 10000, 10000, None, equity=10000, day_start_equity=10000)
    d, _, extra = rt.harness_decision(empty, harness_ctx({"USD_JPY": features()}, settings=settings, jev=Jev()), [])
    assert d["exit_method"] == "target" and d["take_pips"] == 48.0
    assert extra["logic"]["exit_choice"] == {"by": "jev", "method": "target", "confidence": 0.8, "probabilities": None}
    d, _, extra = rt.harness_decision(empty, harness_ctx({"USD_JPY": features()}, settings=settings, jev=None), [])
    assert d["exit_method"] == "chandelier" and "error" in extra["logic"]["exit_choice"]  # 聞けなければ決まった手法


def test_live_watch_rows_mark_harness_as_rules_managed():
    events = [
        {"kind": "live_open", "account": "harness", "symbol": "USD_JPY", "side": "BUY", "size": 300, "position_id": 9,
         "price": 158.0, "at": "2026-11-04T09:00:00+09:00"},
        {"kind": "live_protect", "account": "harness", "position_id": 9, "stop": 157.84, "take": 158.32},
        {"kind": "live_decision", "account": "harness", "at": "2026-11-04T09:30:00+09:00", "decision": {"action": "hold"},
         "logic": {"exit_level": 158.1}},
    ]
    [row] = rt.live_watch_rows(events)
    assert row["managed"] == "rules" and row["exit_level"] == 158.1
