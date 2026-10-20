#!/bin/sh
# Cloud Run での起動。台帳を Cloud Storage から戻し、複製を続けながら GUI を動かす。
#
#   state/       … Cloud Storage を直接つないだもの（設定・AI の記録・キルスイッチなど）
#   data/        … Cloud Storage を読み取り専用でつないだもの（相場データ）
#   $JQBT_LEDGER_DIR … 台帳。コンテナの中のディスク。Litestream で複製する
set -eu
CONFIG=/app/deploy/litestream.yml
mkdir -p "$JQBT_LEDGER_DIR"

for name in ops_paper ops_live; do
  db="$JQBT_LEDGER_DIR/$name.db"
  litestream restore -config "$CONFIG" -if-db-not-exists -if-replica-exists "$db"
  # 複製がまだ無いとき（初回）は、手元から上げた台帳（state/seed/）から始める
  if [ ! -f "$db" ] && [ -f "/app/state/seed/$name.db" ]; then
    echo "台帳 $name を state/seed/ から作ります（初回）"
    cp "/app/state/seed/$name.db" "$db"
  fi
done

exec litestream replicate -config "$CONFIG" \
  -exec "python scripts/gui.py --server.port=${PORT:-8080} --server.address=0.0.0.0"
