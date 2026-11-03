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
