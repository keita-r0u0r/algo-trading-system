"""操作がない画面の接続を切る部品。ブラウザでの動きは手で確かめた。ここでは埋め込む内容を確かめる。"""

from __future__ import annotations

from jqbt.interfaces.gui.idle import idle_script


def test_passes_auto_refresh_state_and_limit():
    assert "__jqbtAutoRefresh = true" in idle_script(True)
    assert "__jqbtAutoRefresh = false" in idle_script(False)
    assert "600000" in idle_script(False)
    assert "60000" in idle_script(False, minutes=1)


def test_inner_script_cannot_close_the_outer_script_early():
    """中身の閉じタグがそのまま入ると、外側の script がそこで終わって壊れる。"""
    assert idle_script(False).count("</script>") == 1


def test_placeholders_are_filled():
    html = idle_script(False)
    assert "EVENTS" not in html and "IDLE_MS" not in html and "CHECK_MS" not in html
