"""画面に出す値。秘密を出さないこと、制限の変更の向き、データの鮮度を固定する。"""

from __future__ import annotations

import datetime as dt

from jqbt.core.gates import Check, Mode
from jqbt.core.risk import RiskLimits
from jqbt.interfaces import viewmodels as vm


def test_secrets_are_shown_only_as_set_or_unset():
    assert vm.secret_state("abc-very-secret") == "設定済み"
    assert vm.secret_state("") == "未設定"
    assert vm.secret_state(None) == "未設定"


def test_freshness_flags_data_too_old_for_live_signals():
    fresh = vm.freshness(dt.date(2026, 7, 14), dt.date(2026, 7, 16))
    stale = vm.freshness(dt.date(2026, 7, 16), dt.date(2026, 10, 17))
    assert fresh["ok"] and not stale["ok"]
    assert "古すぎる" in stale["message"] and stale["age_days"] == 93


def test_unimplemented_modes_are_listed_but_not_available():
    options = {o["mode"]: o for o in vm.mode_options()}
    assert options[Mode.PAPER]["available"]
    assert not options[Mode.AUTO]["available"] and "未実装" in options[Mode.AUTO]["note"]


def test_limit_change_tells_whether_it_applies_now_or_later():
    base = RiskLimits(max_order_value=5_000)
    tighter = vm.limit_change(base, RiskLimits(max_order_value=3_000))
    looser = vm.limit_change(base, RiskLimits(max_order_value=9_000))
    same = vm.limit_change(base, base)

    assert not tighter["loosening"] and "すぐ" in tighter["timing"]
    assert looser["loosening"] and "24時間後" in looser["timing"]
    assert not same["changed"]
    assert {r["向き"] for r in looser["rows"]} == {"緩める", "—"}


def test_status_cards_summarize_the_account():
    status = {
        "mode": Mode.PAPER, "strategy": "regime_hold", "equity": 10_385.4, "cash": 2_961.0,
        "positions": [{"symbol": "9432", "quantity": 13, "price": 153.0, "value": 1_989.0}],
        "gates": [Check("継続日数", True, ""), Check("約定数", False, "")], "gates_passed": False,
    }
    cards = {c["label"]: c for c in vm.status_cards(status)}
    assert cards["評価額"]["value"] == "10,385円"
    assert cards["増額の条件"]["value"] == "未達" and cards["増額の条件"]["note"] == "1 / 2"


def test_fill_rows_show_adverse_slippage_as_positive():
    rows = vm.fill_rows([
        {"date": "2026-07-01", "side": "buy", "symbol": "A", "quantity": 1, "price": 101.0, "fee": 0, "ref_open": 100.0, "source": "manual"},
        {"date": "2026-07-02", "side": "sell", "symbol": "A", "quantity": 1, "price": 99.0, "fee": 0, "ref_open": 100.0, "source": "manual"},
        {"date": "2026-07-03", "side": "buy", "symbol": "B", "quantity": 1, "price": 50.0, "fee": 0, "ref_open": None, "source": "manual"},
    ])
    assert rows[0]["始値とのずれ(%)"] == 1.0
    assert rows[1]["始値とのずれ(%)"] == 1.0
    assert rows[2]["始値とのずれ(%)"] is None


# --- 判断する者の動き具合 -----------------------------------------------------------
def _decision(at, account="ai", **extra):
    return {"kind": "live_decision", "at": at.isoformat(), "account": account, "decision": {"action": "hold"}, **extra}


def test_decider_health_tells_real_ai_from_default_hold():
    import datetime as dt

    from jqbt.interfaces.viewmodels import decider_health

    now = dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    m = dt.timedelta(minutes=1)
    real = [_decision(now - 10 * m, model="gemini")]
    assert decider_health(real + [_decision(now - 5 * m)], "ai", now)["state"] == "ok"
    # 「持ち続ける」は並んでいても、AI が実際には呼べていない
    stale = [_decision(now - 60 * m, model="gemini")] + [_decision(now - k * m, error="PermissionDenied") for k in (15, 10, 5)]
    health = decider_health(stale, "ai", now)
    assert health["state"] == "bad" and "AI を呼べていない" in health["text"] and "PermissionDenied" in health["text"]
    assert health["failed_1h"] == 3
    assert decider_health([_decision(now - 30 * m, model="gemini")], "ai", now)["text"] == "判断の回が止まっている"
    assert decider_health([], "ai", now, market_open=False)["state"] == "info"


def test_decider_health_for_rule_logic():
    import datetime as dt

    from jqbt.interfaces.viewmodels import decider_health

    now = dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    ok = [_decision(now - dt.timedelta(minutes=3), account="carry_trend_USD_JPY", logic={"target": 1})]
    assert decider_health(ok, "carry_trend_USD_JPY", now)["state"] == "ok"
    broken = [_decision(now - dt.timedelta(minutes=3), account="carry_trend_USD_JPY")]
    assert "ロジック" in decider_health(broken, "carry_trend_USD_JPY", now)["text"]


def test_explain_carry_hold_is_readable():
    from jqbt.interfaces.viewmodels import action_label, explain_decision

    e = {"kind": "live_decision", "account": "carry_trend_USD_JPY", "decision": {"action": "hold"},
         "logic": {"carry_percent": 1.88, "last_close": 157.588, "average_50d": 157.441, "target": 1, "held": 1}}
    text = explain_decision(e, holding=True)
    assert "金利差は +1.88%" in text and "平均 157.441 より上" in text and "持ち続ける" in text
    assert action_label(e["decision"], True) == "持ち続ける"
    assert action_label(e["decision"], False) == "建てない（様子見）"


def test_explain_ai_default_hold_and_holdings():
    from jqbt.interfaces.viewmodels import explain_decision, holdings_over_time

    assert "呼べなかった" in explain_decision({"error": "PermissionDenied", "decision": {"action": "hold"}})
    assert explain_decision({"note": "前回 AI を呼んだのは 12:00", "decision": {"action": "hold"}}) == "前回 AI を呼んだのは 12:00"
    assert "呼ぶ回ではない" in explain_decision({"note": "AI を呼ぶ回ではない（古い版）", "decision": {"action": "hold"}})
    events = [{"kind": "live_decision", "account": "ai"},
              {"kind": "live_open", "account": "ai", "position_id": 1},
              {"kind": "live_decision", "account": "ai"},
              {"kind": "live_gone", "account": "ai"},
              {"kind": "live_decision", "account": "ai"}]
    assert holdings_over_time(events) == [False, None, True, None, False]


def test_ai_next_looks_lists_every_trigger():
    import datetime as dt

    from jqbt.interfaces import viewmodels as vm

    jst = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime(2026, 11, 3, 21, 34, tzinfo=jst)
    events = [{"kind": "live_decision", "account": "ai", "model": "m", "at": "2026-11-03T21:26:00+09:00",
               "logged_at": "2026-11-03T12:26:40+00:00"},
              {"kind": "live_decision", "account": "ai", "model": "m", "note": "呼ばなかった", "at": "2026-11-03T21:31:00+09:00"}]
    last = vm.ai_last_real_call(events)
    assert last == dt.datetime(2026, 11, 3, 21, 26, 40, tzinfo=jst)
    looks = vm.ai_next_looks({"stop": 212.017, "take": 212.617}, 212.118, now, last,
                             {"near_pips": 3, "move_pips": 15, "window_minutes": 5}, "22:00")
    assert looks[0] == "定期: 21:41 ごろ（15分ごと）"
    assert "212.047 に下がったら" in looks[1] and "212.587 に上がったら" in looks[2]
    assert looks[-1] == "時間帯の切り替わり: 22:00"


def test_month_outlook_projects_pace_and_needs_data_for_chance():
    import datetime as dt

    from jqbt.interfaces import viewmodels as vm

    jst = dt.timezone(dt.timedelta(hours=9))
    pts = [(dt.datetime(2026, 11, 2, 19, 0, tzinfo=jst), 10_000.0), (dt.datetime(2026, 11, 3, 19, 0, tzinfo=jst), 10_100.0)]
    o = vm.month_outlook(pts, dt.datetime(2026, 11, 3, 19, 0, tzinfo=jst))
    assert o["goal"] == 15_000 and o["elapsed_days"] == 1.0
    assert abs(o["daily_pace"] - 0.01) < 1e-9 and o["projected"] > o["current"]
    assert "chance" not in o  # 1日分では確からしさを出さない
    days = [dt.datetime(2026, 11, d, 23, 0, tzinfo=jst) for d in (2, 3, 4, 5, 6, 9, 10)]
    pts = [(t, 10_000 * 1.02 ** i) for i, t in enumerate(days)]
    o = vm.month_outlook(pts, days[-1])
    assert 0 <= o["chance"] <= 1 and o["days_of_data"] >= 5


def test_monthly_ai_cost():
    import datetime as dt

    from jqbt.interfaces import viewmodels as vm

    jst = dt.timezone(dt.timedelta(hours=9))
    recs = [{"kind": "call", "at": "2026-11-02T01:00:00+00:00", "cost_usd": 0.1},
            {"kind": "call", "at": "2026-10-30T01:00:00+00:00", "cost_usd": 9.0},
            {"kind": "rating", "at": "2026-11-02T01:00:00+00:00"}]
    c = vm.monthly_ai_cost(recs, dt.datetime(2026, 11, 3, 0, 0, tzinfo=jst))
    assert abs(c["spent"] - 0.1) < 1e-9 and c["projected"] > c["spent"]


def _trade_events():
    return [
        {"kind": "live_open", "account": "harness", "position_id": 1, "symbol": "USD_JPY", "side": "BUY", "at": "2026-11-04T09:00:00+09:00"},
        {"kind": "snapshot", "positions": [{"positionId": 1, "lossGain": "60"}]},
        {"kind": "execution", "settleType": "CLOSE", "positionId": 1, "lossGain": "45"},
        {"kind": "live_open", "account": "harness", "position_id": 2, "symbol": "GBP_JPY", "side": "SELL", "at": "2026-11-04T11:00:00+09:00"},
        {"kind": "execution", "settleType": "CLOSE", "positionId": 2, "lossGain": "-30"},
        {"kind": "live_open", "account": "ai", "position_id": 3, "symbol": "USD_JPY", "side": "BUY", "at": "x"},
        {"kind": "execution", "settleType": "CLOSE", "positionId": 3, "lossGain": "-99"},
    ]


def test_decider_scorecard_puts_payoff_and_kept_profit_first():
    from jqbt.interfaces import viewmodels as vm

    c = vm.decider_scorecard(_trade_events(), "harness")
    assert c["trades"] == 2 and c["total"] == 15.0 and c["win_rate"] == 0.5
    assert c["avg_win"] == 45.0 and c["avg_loss"] == -30.0 and c["payoff"] == 1.5
    assert c["peaked"] == 1 and c["kept_of_peak"] == 0.75
    assert vm.decider_scorecard([], "harness")["payoff"] is None


def test_filter_shadow_rows_pair_the_answer_with_the_result():
    from jqbt.interfaces import viewmodels as vm

    events = _trade_events() + [{"kind": "live_decision", "account": "harness", "approved": True,
                                 "at": "2026-11-04T09:00:00+09:00",
                                 "decision": {"action": "open_long", "symbol": "USD_JPY"},
                                 "logic": {"filter": {"provider": "jev", "skip_probability": 0.2, "veto": False}}}]
    rows = vm.filter_shadow_rows(events)
    assert rows[0]["skip_probability"] == 0.2 and rows[0]["said_skip"] is False and rows[0]["pnl"] == 45.0
    assert rows[1]["provider"] is None and rows[1]["pnl"] == -30.0


def test_harness_reason_reads_the_evidence():
    from jqbt.interfaces import viewmodels as vm

    e = {"account": "harness", "decision": {"action": "hold"},
         "logic": {"evidence": {"USD_JPY": ["上向きの流れ", "押していない"]}}}
    assert vm.explain_decision(e) == "建てない。ドル円: 押していない"
    e = {"account": "harness", "decision": {"action": "hold"}, "logic": {"evidence": ["含み損益 +3.0 pips"]}}
    assert vm.explain_decision(e).startswith("持ち続ける")


def test_changelog_parses_and_every_entry_has_a_title():
    from pathlib import Path

    from jqbt.interfaces import viewmodels as vm

    text = "# x\n\n## #53 · 2026-11-04 · 新しい\n- **売買**: a\n\n## #52 · 2026-11-03 · 前の\n- b\n"
    log = vm.parse_changelog(text)
    assert log[53] == {"date": "2026-11-04", "title": "新しい", "body": "- **売買**: a"} and log[52]["body"] == "- b"
    real = vm.parse_changelog((Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert real and all(e["title"] and e["date"] for e in real.values())


def test_release_rows_are_newest_first_with_changes():
    from jqbt.interfaces import viewmodels as vm

    releases = [{"version": "a", "verified_at": "2026-11-03T22:30:00+09:00", "result": "backfill", "prs": [51]},
                {"version": "b", "verified_at": "2026-11-04T08:08:00+09:00", "result": "ok", "previous": "a",
                 "prs": [52], "titles": {"52": "題（GitHub）"}}]
    rows = vm.release_rows(releases, {52: {"title": "題", "body": "- 中身", "date": "x"}})
    assert [r["version"] for r in rows] == ["b", "a"]
    assert rows[0]["changes"] == [{"pr": 52, "title": "題", "body": "- 中身"}]
    assert rows[1]["changes"][0]["title"] == ""


def test_blocked_opens_lists_recent_rejected_entries():
    import datetime as dt

    from jqbt.interfaces import viewmodels as vm

    jst = dt.timezone(dt.timedelta(hours=9))
    events = [{"kind": "live_decision", "at": "2026-11-04T06:40:00+09:00", "account": "ai", "approved": False,
               "decision": {"action": "open_short", "symbol": "EUR_JPY"}, "rejected": "古い"},
              {"kind": "live_decision", "at": "2026-11-04T07:55:00+09:00", "account": "ai", "approved": False,
               "decision": {"action": "open_short", "symbol": "GBP_JPY"}, "rejected": "スプレッドが広い（17.9 pips > 1）"},
              {"kind": "live_decision", "at": "2026-11-04T08:00:00+09:00", "account": "ai", "approved": False,
               "decision": {"action": "hold"}}]
    rows = vm.blocked_opens(events, dt.datetime(2026, 11, 4, 8, 10, tzinfo=jst))
    assert [r["symbol"] for r in rows] == ["GBP_JPY"] and "スプレッド" in rows[0]["reason"]
