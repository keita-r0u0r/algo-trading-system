"""依存方向と副作用の機械的な検査。

ドキュメントに「core は純粋です」と書いても、半年後には破られている。
**書いた瞬間に落ちるテスト**にしておかないと守られない。

AST で解析する。文字列一致だと docstring やコメント中の語まで拾って
誤検知するため（「``datetime.now()`` を書かない」という説明文が
そのまま違反として検出される、など）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "jqbt"

LAYERS = ("core", "application", "infrastructure", "interfaces", "strategies")

#: 各層が import してよい内部パッケージ
ALLOWED: dict[str, set[str]] = {
    # core は誰にも依存しない
    "core": set(),
    # application は core と自分の Port だけ
    "application": {"core"},
    # infrastructure は core を使ってよい（DTOの変換などで必要）
    "infrastructure": {"core"},
    # 戦略は core と、Port の型だけ
    "strategies": {"core", "application"},
    # interfaces は組み立て役なので全部見てよい
    "interfaces": {"core", "application", "infrastructure", "strategies"},
}

#: core で呼んではいけない関数
FORBIDDEN_CALLS_IN_CORE = {"now", "today", "utcnow", "open", "print", "input"}

#: core で import してはいけないモジュール
FORBIDDEN_IMPORTS_IN_CORE = {
    "httpx",
    "requests",
    "urllib",
    "sqlite3",
    "sqlalchemy",
    "random",
    "pathlib",
    "os",
    "logging",
}


def python_files(layer: str) -> list[Path]:
    directory = SRC / layer
    if not directory.exists():
        return []
    return sorted(p for p in directory.rglob("*.py") if p.name != "__init__.py")


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_roots(tree: ast.Module) -> set[str]:
    """import しているモジュールの先頭要素。"""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # 相対 import。jqbt 内部のどのパッケージを指すかを解決する
                if node.module:
                    roots.add(node.module.split(".")[0])
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots


def internal_packages(path: Path, tree: ast.Module) -> set[str]:
    """jqbt 内部のどの層を import しているか。"""
    layer_root = path.relative_to(SRC).parts[0]
    depth_in_layer = len(path.relative_to(SRC).parts) - 1  # ファイル名を除く

    packages: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            # level=1 は同じパッケージ、level=2 は jqbt 直下
            if node.level - 1 > depth_in_layer:
                # jqbt 直下より上 → 他の層
                target = (node.module or "").split(".")[0]
                if target in LAYERS:
                    packages.add(target)
            elif node.level - 1 == depth_in_layer and node.module:
                target = node.module.split(".")[0]
                if target in LAYERS:
                    packages.add(target)
        elif isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "jqbt" and len(parts) > 1 and parts[1] in LAYERS:
                packages.add(parts[1])

    packages.discard(layer_root)
    return packages


def called_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


# ----------------------------------------------------------------------
@pytest.mark.parametrize("layer", LAYERS)
def test_dependency_direction(layer: str):
    """層をまたぐ import が、許された向きだけであること。"""
    allowed = ALLOWED[layer]
    for path in python_files(layer):
        used = internal_packages(path, parse(path))
        violations = used - allowed
        assert not violations, (
            f"{path.relative_to(SRC)} が {sorted(violations)} を import しています。"
            f"{layer} が依存してよいのは {sorted(allowed) or '（なし）'} だけです"
        )


def test_core_has_no_side_effects():
    """core が現在時刻・ファイル・標準出力に触っていないこと。

    ここが破られると、同じ入力でも結果が変わるようになり、
    ウォークフォワードの再現性が静かに失われる。
    """
    for path in python_files("core"):
        names = called_names(parse(path))
        violations = names & FORBIDDEN_CALLS_IN_CORE
        assert not violations, (
            f"{path.relative_to(SRC)} が {sorted(violations)} を呼んでいます。"
            "core は純粋でなければなりません"
        )


def test_core_does_not_import_io_libraries():
    for path in python_files("core"):
        roots = imported_roots(parse(path))
        violations = roots & FORBIDDEN_IMPORTS_IN_CORE
        assert not violations, (
            f"{path.relative_to(SRC)} が {sorted(violations)} を import しています。"
            "core は外界に触れてはいけません"
        )


def test_application_does_not_import_infrastructure():
    """application が具体的なデータ源を知らないこと。

    知ってしまうと、データ源を替えるたびに application を書き換えることになる。
    """
    for path in python_files("application"):
        roots = imported_roots(parse(path))
        assert "infrastructure" not in roots
        for forbidden in ("httpx", "pyarrow"):
            assert forbidden not in roots, (
                f"{path.relative_to(SRC)} が {forbidden} を import しています。"
                "外部I/Oは infrastructure に置いてください"
            )


def test_core_contracts_are_frozen():
    """core.contracts のデータクラスが不変であること。

    可変だと、戦略に渡した Portfolio を戦略側が書き換えて
    会計を壊せてしまう。
    """
    tree = parse(SRC / "core" / "contracts.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        decorators = [d for d in node.decorator_list if isinstance(d, ast.Call)]
        dataclasses = [
            d
            for d in decorators
            if (isinstance(d.func, ast.Name) and d.func.id == "dataclass")
            or (isinstance(d.func, ast.Attribute) and d.func.attr == "dataclass")
        ]
        for decorator in dataclasses:
            frozen = [
                kw for kw in decorator.keywords if kw.arg == "frozen"
            ]
            assert frozen and getattr(frozen[0].value, "value", False) is True, (
                f"contracts.{node.name} が frozen ではありません"
            )


def test_every_layer_has_files():
    """層の構成が壊れていないこと（リファクタの取りこぼし検出）。"""
    for layer in ("core", "application", "infrastructure"):
        assert python_files(layer), f"{layer} にファイルがありません"


def test_no_stale_top_level_modules():
    """層に移したはずのモジュールが jqbt 直下に残っていないこと。"""
    stale = {"engine.py", "costs.py", "metrics.py", "dataset.py", "jquants.py", "synthetic.py"}
    present = {p.name for p in SRC.glob("*.py")}
    assert not (present & stale), f"移動し忘れ: {sorted(present & stale)}"
