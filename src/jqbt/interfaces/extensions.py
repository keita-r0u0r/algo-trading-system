"""拡張パーツの読み込みと、差し込み口の定義。

本体は拡張パーツを **名前でだけ** 知っている（``importlib`` で読み込む）。import 文では参照しないので、
パーツを消しても、無効にしても、本体はそのまま動く。

## 差し込み口

  * GUI の画面を足す（``Extension.gui_pages``）。描画の関数は ``GuiContext`` を受け取る
  * 秘密情報を一覧に足す（パーツが読み込み時に ``register_secret`` を呼ぶ）
  * 売買候補を出す（``OperationsService.submit_orders``。ペーパー運用でのみ、リスク制限を必ず通る）

## パーツの約束

``jqbt.extensions.<name>`` に ``extension()`` 関数を置き、``Extension`` を満たすものを返す。
"""

from __future__ import annotations

import datetime as dt
import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

ENV_NAME = "JQBT_EXTENSIONS"


@dataclass
class GuiContext:
    """GUI の画面に渡す、いまの状況。拡張パーツはこれだけを使って描く。"""

    profile: str
    ops: Any  # application.operations.OperationsService
    index: Any  # application.panel_index.PanelIndex
    latest: dt.date
    today: dt.date
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuiPage:
    title: str
    render: Callable[[GuiContext], None]


class Extension(Protocol):
    name: str
    title: str
    description: str

    def available(self) -> tuple[bool, str]:
        """使える状態か（依存ライブラリ、秘密情報）。使えないときは理由を返す。"""
        ...

    def gui_pages(self) -> list[GuiPage]: ...


@dataclass(frozen=True)
class LoadResult:
    extensions: list[Any]
    #: 読み込めなかったパーツと理由。読み込めなくても本体は止めない
    errors: dict[str, str]


def enabled_names(environ: Mapping[str, str] | None = None) -> list[str]:
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_NAME, "")
    return [name.strip() for name in raw.split(",") if name.strip()]


def load(names: list[str]) -> LoadResult:
    extensions, errors = [], {}
    for name in names:
        if not name.isidentifier():
            errors[name] = "名前が不正です"
            continue
        try:
            module = importlib.import_module(f"jqbt.extensions.{name}")
            extensions.append(module.extension())
        except ModuleNotFoundError as exc:
            errors[name] = f"見つかりません（{exc.name}）"
        except Exception as exc:  # パーツの不具合で本体を止めない
            errors[name] = f"{type(exc).__name__}: {exc}"
    return LoadResult(extensions, errors)
