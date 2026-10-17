""".env の読み込みの検証。

README は「キーを .env に書く」と案内しているのに、コードが
``os.getenv`` しか見ていなければ **キーは永久に読まれない**。
しかも症状は「未設定です」なので、キーの発行をやり直す方向に迷う。
その取り合わせを防ぐためのテスト。
"""

from __future__ import annotations

import os

import pytest

from jqbt.infrastructure.env import find_env, load_env, parse_env


@pytest.fixture(autouse=True)
def restore_environment():
    """``load_env`` は os.environ を直接書くので、monkeypatch では戻らない。

    戻さないと、あとに走るテストが「キーが設定済み」の環境を見てしまう。
    """
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_plain_key_value():
    assert parse_env("JQUANTS_API_KEY=abc123") == {"JQUANTS_API_KEY": "abc123"}


def test_comments_and_blank_lines_are_ignored():
    text = "# コメント\n\nJQUANTS_PLAN=free\n  # 字下げしたコメント\n"
    assert parse_env(text) == {"JQUANTS_PLAN": "free"}


def test_export_prefix_is_accepted():
    """README の `export JQUANTS_API_KEY=...` をそのまま貼っても動くこと。"""
    assert parse_env("export JQUANTS_API_KEY=abc") == {"JQUANTS_API_KEY": "abc"}


@pytest.mark.parametrize("quoted", ['"abc"', "'abc'"])
def test_surrounding_quotes_are_stripped(quoted):
    """ダッシュボードからのコピペで引用符が付くことがある。

    外さないとキーが `"abc"` になり、401 の原因が分かりにくい。
    """
    assert parse_env(f"JQUANTS_API_KEY={quoted}") == {"JQUANTS_API_KEY": "abc"}


def test_malformed_lines_do_not_discard_the_rest():
    """1行の書き間違いでファイル全体を無効にしない。"""
    assert parse_env("ごみ\nJQUANTS_API_KEY=abc\n") == {"JQUANTS_API_KEY": "abc"}


def test_empty_value_is_kept_as_empty():
    """``.env.example`` をコピーしただけの状態を「設定済み」にしないこと。"""
    assert parse_env("JQUANTS_API_KEY=") == {"JQUANTS_API_KEY": ""}


def test_existing_environment_wins(tmp_path, monkeypatch):
    """``KEY=x python scripts/...`` での一時的な差し替えを壊さないこと。"""
    (tmp_path / ".env").write_text("JQUANTS_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("JQUANTS_API_KEY", "from-shell")

    load_env(tmp_path)
    assert os.environ["JQUANTS_API_KEY"] == "from-shell"


def test_value_is_loaded_when_the_variable_is_absent(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("JQUANTS_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)

    assert load_env(tmp_path) == tmp_path / ".env"
    assert os.environ["JQUANTS_API_KEY"] == "from-file"


def test_env_is_found_in_a_parent_directory(tmp_path):
    """どのディレクトリから起動しても効くこと。"""
    (tmp_path / ".env").write_text("JQUANTS_PLAN=light\n", encoding="utf-8")
    nested = tmp_path / "scripts" / "deep"
    nested.mkdir(parents=True)

    assert find_env(nested) == tmp_path / ".env"


def test_missing_env_is_not_an_error(tmp_path):
    """.env がなくても落ちないこと（合成データだけで動かせる）。"""
    assert load_env(tmp_path) is None
