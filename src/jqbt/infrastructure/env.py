""".env の読み込み（Imperative Shell）。

依存を増やさないために python-dotenv は使わない。必要なのは
``KEY=VALUE`` を読むことだけで、補間や多段の設定解決はいらない。

**既にある環境変数は上書きしない。** ``JQUANTS_API_KEY=... python scripts/...``
のように一回だけ差し替えたいときに、ファイルが勝つと驚くため。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILENAME = ".env"


def find_env(start: Path | None = None) -> Path | None:
    """``start`` から上に辿って最初に見つかった .env を返す。

    スクリプトをどのディレクトリから起動しても効くようにする。
    """
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / ENV_FILENAME
        if candidate.is_file():
            return candidate
    return None


def parse_env(text: str) -> dict[str, str]:
    """``KEY=VALUE`` の行だけを読む。

    - ``#`` で始まる行と空行は無視
    - ``export KEY=VALUE`` も受ける
    - 値を囲む引用符は外す（コピペで付いてくることがある）
    - ``=`` を含まない行は黙って捨てる（書き間違いで全体を落とさない）
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env(start: Path | None = None) -> Path | None:
    """.env を環境変数に読み込む。読んだファイルのパスを返す。

    スクリプトの入口で1回だけ呼ぶ。core / application からは呼ばない。
    """
    path = find_env(start)
    if path is None:
        return None
    for key, value in parse_env(path.read_text(encoding="utf-8")).items():
        # 既にある値を尊重する（コマンドラインでの一時的な差し替えを壊さない）
        os.environ.setdefault(key, value)
    return path
