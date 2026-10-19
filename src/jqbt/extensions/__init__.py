"""拡張パーツ。本体（core / application / infrastructure / strategies）からは import しない。

有効にするには、環境変数 ``JQBT_EXTENSIONS`` にパーツの名前をカンマ区切りで書く（例: ``ai``）。
読み込みは ``interfaces/extensions.py`` が名前で行う。新しいパーツの作り方は docs/extensions.md。
"""
