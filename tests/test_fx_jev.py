from jqbt.application import fx_jev as fj


def answer(usd=3.4, gbp=2.2, conf=0.7, risk=2.0, method="chandelier"):
    return {"answers": {"outlook_USD_JPY": {"score": usd, "confidence": conf},
                        "outlook_GBP_JPY": {"score": gbp, "confidence": conf},
                        "risk": {"score": risk}, "exit_method": {"choice": method}}}


def test_entry_questions_ask_each_pairs_outlook():
    q = fj.entry_questions(["USD_JPY", "GBP_JPY"])
    assert set(q) == {"outlook_USD_JPY", "outlook_GBP_JPY", "risk", "exit_method"}
    assert len(q["outlook_USD_JPY"]["criteria"]) == 5 and len(q["risk"]["criteria"]) == len(fj.RISK_LEVELS)


def test_read_entry_trades_the_most_tilted_pair_only_when_clear():
    e = fj.read_entry(answer())
    assert e["open"] and e["symbol"] == "USD_JPY" and e["side"] == 1 and e["risk_fraction"] == 0.01
    e = fj.read_entry(answer(usd=2.5, gbp=0.6))
    assert e["open"] and e["symbol"] == "GBP_JPY" and e["side"] == -1
    assert not fj.read_entry(answer(usd=2.8, gbp=1.5))["open"]  # 傾きが足りない
    assert not fj.read_entry(answer(conf=0.3))["open"]
    assert fj.read_entry(answer(risk=9))["risk_fraction"] == 0.02
    assert fj.read_entry(answer(method="?"))["exit_method"] == "chandelier"
    assert not fj.read_entry({"answers": {}})["open"]


def test_stop_comes_from_the_machine_not_jev():
    assert fj.stop_pips(20) == 16.0 and fj.stop_pips(5) == 12.0 and fj.stop_pips(None) == 12.0 and fj.stop_pips(200) == 40.0


def test_read_hold():
    assert fj.read_hold({"answers": {"close": {"noul": 0.8}}}) == (True, 0.8)
    assert fj.read_hold({"answers": {"close": {"noul": 0.5}}}) == (False, 0.5)
    assert fj.read_hold({}) == (False, None)
