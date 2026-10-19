"""拡張パーツの読み込み。無効でも、壊れていても、本体は止まらない。"""

from __future__ import annotations

from jqbt.infrastructure.secrets import SECRETS, EnvSecrets
from jqbt.interfaces.extensions import enabled_names, load


def test_no_extensions_by_default():
    assert enabled_names({}) == []
    assert enabled_names({"JQBT_EXTENSIONS": " ai , ,"}) == ["ai"]


def test_missing_or_invalid_extensions_are_reported_not_raised():
    result = load(["does_not_exist", "bad-name"])
    assert result.extensions == []
    assert set(result.errors) == {"does_not_exist", "bad-name"}


def test_ai_extension_loads_adds_a_page_and_registers_its_secret():
    result = load(["ai"])
    assert not result.errors
    (ext,) = result.extensions
    assert ext.name == "ai"
    assert [page.title for page in ext.gui_pages()] == ["AI（Claude）"]
    # 読み込んだときに、自分の秘密情報を一覧に足している
    assert "anthropic_api_key" in SECRETS
    assert EnvSecrets({"ANTHROPIC_API_KEY": "k"}).get("anthropic_api_key") == "k"
