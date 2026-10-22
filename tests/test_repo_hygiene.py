"""公開リポジトリに入れてはいけないものが、Git に入っていないこと。

2026-10-21、別のブランチで作業したときに、メールアドレス入りの infra/terraform.tfvars と
OpenTofu のプラグインがコミットに入りかけた（push の失敗で気づいた）。手で気をつけるのでなく、テストで止める。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)terraform\.tfvars$"),
    re.compile(r"\.tfstate"),
    re.compile(r"(^|/)\.terraform/"),
    re.compile(r"^data/"),
    re.compile(r"^state/"),
    re.compile(r"\.db$"),
]
#: 個人のメールアドレス（例として書くときは example.com を使う）
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(gmail|yahoo|icloud|outlook|hotmail)\.[a-z.]+")


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("Git のリポジトリの外（Docker のイメージなど）")
    return [line for line in out.splitlines() if line]


def test_no_secrets_or_private_data_are_tracked():
    bad = [f for f in tracked_files() if any(p.search(f) for p in FORBIDDEN_PATHS)]
    assert not bad, f"Git に入れてはいけないファイル: {bad}"


def test_no_personal_email_in_tracked_text():
    hits = []
    for name in tracked_files():
        path = ROOT / name
        if path.suffix in {".png", ".jpg", ".parquet", ".db"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if EMAIL.search(text):
            hits.append(name)
    assert not hits, f"個人のメールアドレスらしきものがある: {hits}"
