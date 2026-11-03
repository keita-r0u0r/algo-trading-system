from jqbt.application import fx_filter as ff


def test_settings_default_and_bad_values_fall_back():
    s = ff.parse_settings(None)
    assert s.enabled("ai") and s.enabled("harness") and s.filter.mode == "shadow" and s.filter.provider == "jev"
    s = ff.parse_settings({"deciders": {"ai": False}, "filter": {"mode": "nope", "provider": "x", "threshold": "y"}})
    assert not s.enabled("ai") and s.enabled("harness") and s.stage("harness") == "live"
    s2 = ff.parse_settings({"deciders": {"jev": "shadow", "harness": "nope", "ai": True}})
    assert s2.stage("jev") == "shadow" and s2.enabled("jev") and s2.stage("harness") == "live" and s2.stage("ai") == "live"
    assert (s.filter.mode, s.filter.provider, s.filter.threshold) == ("shadow", "jev", 0.6)
    assert ff.parse_settings({"filter": {"threshold": 5}}).filter.threshold == 0.95
    assert ff.parse_settings(s.to_dict()) == s


def jev(skip, score=2.0):
    return {"model": "jev-1.13.0", "answers": {"skip": {"type": "noul", "noul": skip},
                                               "quality": {"type": "score", "score": score, "confidence": 0.7}},
            "usage": {"input_tokens": 1000, "output_tokens": 5}}


def test_jev_verdict_uses_the_threshold():
    settings = ff.FilterSettings(mode="veto", threshold=0.6)
    assert ff.jev_verdict(jev(0.7), settings)["veto"]
    assert not ff.jev_verdict(jev(0.3), settings)["veto"]
    assert ff.jev_verdict({"answers": {}}, settings)["error"]


def test_apply_by_mode():
    veto = ff.jev_verdict(jev(0.9), ff.FilterSettings())
    assert ff.apply(veto, ff.FilterSettings(mode="off")) == (True, "")
    ok, note = ff.apply(veto, ff.FilterSettings(mode="shadow"))
    assert ok and "記録だけ" in note
    ok, note = ff.apply(veto, ff.FilterSettings(mode="veto"))
    assert not ok and "見送り" in note
    ok, _ = ff.apply({"provider": "jev", "error": "キーが無い"}, ff.FilterSettings(mode="veto"))
    assert ok  # 聞けなければ決まりのとおり
    assert ff.apply(ff.gemini_verdict({"skip": True, "reason": "指標の直前"}), ff.FilterSettings(mode="veto"))[0] is False


def test_jev_request_shape_and_cost():
    q = ff.jev_questions()
    assert q["skip"]["type"] == "noul" and q["quality"]["type"] == "score" and len(q["quality"]["criteria"]) == 4
    state = ff.review_state({"symbol": "USD_JPY"}, {"price": 1, "recent_1min_close": [1], "summary": ["x"]}, [])
    assert "recent_1min_close" not in state["market_features"] and "summary" not in state["market_features"]
    assert ff.jev_cost_usd({"input_tokens": 1_000_000}) == 0.042


def test_exit_selector_settings_and_jev_choice():
    s = ff.parse_settings({"exits": {"selector": "jev", "method": "structure"}})
    assert s.exits.selector == "jev" and s.exits.method == "structure"
    assert ff.parse_settings({"exits": {"selector": "x", "method": "y"}}).exits == ff.ExitSettings()
    q = ff.jev_questions(skip=False, exit_method=True)
    assert set(q) == {"exit_method"} and set(q["exit_method"]["criteria"]) == set(ff.EXIT_METHODS)
    assert ff.jev_exit_choice({"answers": {"exit_method": {"choice": "breakeven"}}})["method"] == "breakeven"
    assert "error" in ff.jev_exit_choice({"answers": {}})
    assert ff.gemini_exit_choice({"exit_method": "chandelier", "exit_reason": "流れが強い"})["method"] == "chandelier"
