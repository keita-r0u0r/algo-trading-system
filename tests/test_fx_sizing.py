import datetime as dt

from jqbt.application import fx_filter as ff
from jqbt.application import fx_sizing as fz

JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 11, 5, 8, 0, tzinfo=JST)


def trades(account, *pnls):
    events = []
    for i, p in enumerate(pnls, 1):
        events += [{"kind": "live_open", "account": account, "position_id": i, "symbol": "USD_JPY", "side": "BUY",
                    "at": f"2026-11-04T0{i % 10}:00:00+09:00"},
                   {"kind": "execution", "settleType": "CLOSE", "positionId": i, "lossGain": str(p),
                    "at": f"2026-11-04T0{i % 10}:30:00+09:00"}]
    return events


def test_sizing_settings_are_clamped_to_the_users_frame():
    s = ff.parse_settings({"sizing": {"harness": {"max_units": 5000, "max_risk": 0.5}, "ai": {"max_units": 250}}})
    assert s.sizing_for("harness") == ff.Sizing(1000, 0.05)
    assert s.sizing_for("ai") == ff.Sizing(200, 0.05)  # 100通貨単位
    assert ff.parse_settings(None).sizing_for("harness") == ff.Sizing(300, 0.01)
    assert ff.parse_settings(s.to_dict()) == s


def test_stats_weigh_size_of_wins_streaks_and_drawdown():
    st = fz.decider_stats(trades("ai", 50, -30, -30, -30, 100), "ai", 10_000)["all"]
    assert st["trades"] == 5 and st["avg_win_yen"] == 75 and st["avg_loss_yen"] == -30
    assert st["worst_losing_streak"] == 3 and st["max_drawdown_yen"] == 90 and st["max_drawdown_of_equity"] == 0.009


def test_proposal_is_checked_and_not_applied_until_answered():
    settings = ff.FxSettings()
    data = {"summary": "ハーネスは落ち込みが小さい", "changes": [
        {"account": "harness", "max_units": 600, "max_risk": 0.02, "reason": "平均の勝ちが負けの2倍"},
        {"account": "ai", "max_units": 1000, "max_risk": 0.05, "reason": "変えない"},  # 今と同じなので落とす
        {"account": "nope", "max_units": 100, "max_risk": 0.01, "reason": "知らない口座"},
        {"account": "carry_trend_USD_JPY", "max_units": 9000, "max_risk": 0.9, "reason": "枠の外"}]}
    record = fz.proposal_record(data, settings, NOW, "gemini", "test")
    # 枠の外の提案は内側に収める。収めた結果が今と同じなら落とす（金利差の口座は既に枠いっぱい）
    assert [c["account"] for c in record["changes"]] == ["harness"]
    assert record["changes"][0]["to"] == {"max_units": 600, "max_risk": 0.02}
    assert fz.pending([record], []) == record
    assert fz.pending([record], [{"id": record["id"], "answer": "rejected"}]) is None
    applied = fz.apply(settings, record, NOW)
    assert applied.sizing_for("harness") == ff.Sizing(600, 0.02) and settings.sizing_for("harness") == ff.Sizing(300, 0.01)


def test_due_once_a_day_on_weekdays_after_seven():
    assert fz.due([], NOW)
    assert not fz.due([], NOW.replace(hour=6))
    assert not fz.due([], dt.datetime(2026, 11, 7, 9, tzinfo=JST))  # 土曜
    last = [{"kind": "error", "at": (NOW - dt.timedelta(hours=2)).isoformat()}]
    assert not fz.due(last, NOW)  # 失敗した回も数える
    assert fz.due([{"kind": "proposal", "at": (NOW - dt.timedelta(hours=21)).isoformat()}], NOW)


def test_time_metrics_are_added_next_to_the_totals():
    trades = [{"pnl": 100.0, "opened_at": "2026-11-04T09:00:00+09:00", "closed_at": "2026-11-04T11:00:00+09:00",
               "size": 1000, "price": 150.0},
              {"pnl": 500.0, "opened_at": "2026-11-04T09:00:00+09:00", "closed_at": "2026-11-05T09:00:00+09:00",
               "size": 1000, "price": 150.0}]
    m = fz.time_metrics(trades[:1])
    assert m["yen_per_hour"] == 50.0 and m["avg_hold_hours"] == 2.0
    assert m["yen_per_margin_hour"] == round(100 / (6000 * 2) * 10_000, 2)  # 証拠金 1000 × 150 × 4% = 6,000円
    assert fz.time_metrics(trades[1:])["yen_per_hour"] < m["yen_per_hour"]  # 24時間で500円は、2時間で100円より遅い
    assert fz.time_metrics([])["yen_per_hour"] is None
    assert "time" in fz.decider_stats([], "ai", 10_000)
