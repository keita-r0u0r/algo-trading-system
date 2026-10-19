# 拡張パーツ

本体（`core` / `application` / `infrastructure` / `strategies`）とは別に、後から付け外しできる機能です。
本体は拡張パーツを **名前でだけ** 知っていて、import 文では参照しません（`tests/test_architecture.py` が検査）。
パーツを無効にしても、消しても、本体はそのまま動きます。

## 有効にする

```bash
# .env（または環境変数）
JQBT_EXTENSIONS=ai          # カンマ区切りで複数指定できる
```

GUI のサイドバーに、パーツの画面が追加されます。読み込めないパーツがあっても本体は止まらず、サイドバーに理由が出ます。

| パーツ | 内容 | 手順 |
|---|---|---|
| `ai` | AI（Claude）: 検証の解説、運用の見張り、売買ルールの案、ペーパー運用での売買判断 | [ai.md](ai.md) |

## 差し込み口

| 差し込み口 | 使い方 |
|---|---|
| GUI の画面 | `Extension.gui_pages()` で `GuiPage(title, render)` を返す。`render` は `GuiContext`（プロファイル、運用サービス、相場データ、日付）を受け取る |
| 秘密情報 | 読み込み時に `register_secret(SecretName(...))` を呼ぶ。置き場所（GCP Secret Manager / .env）は本体と同じ |
| 売買候補 | `OperationsService.submit_orders(as_of, orders, source=...)`。**ペーパー運用でのみ**受け付け、戦略の候補と同じリスク制限を必ず通る |

拡張パーツは、本体の判断（リスク制限、凍結、キルスイッチ）を迂回できません。発注の口もありません。

## 新しいパーツを作る

`src/jqbt/extensions/<名前>/__init__.py` に `extension()` を置き、次を満たすものを返します。

```python
from jqbt.interfaces.extensions import GuiContext, GuiPage


class MyExtension:
    name = "my"
    title = "私のパーツ"
    description = "何をするか"

    def available(self) -> tuple[bool, str]:
        return True, ""          # 使えないときは (False, "理由")

    def gui_pages(self) -> list[GuiPage]:
        return [GuiPage(self.title, render)]


def render(ctx: GuiContext) -> None:
    import streamlit as st
    st.write(ctx.latest)


def extension() -> MyExtension:
    return MyExtension()
```

追加のライブラリが要るなら、`pyproject.toml` の追加パッケージ（`[project.optional-dependencies]`）に分け、
`import` は使うときに行います（入っていなくても、本体と他のパーツが壊れないように）。
