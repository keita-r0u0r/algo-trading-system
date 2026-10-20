.PHONY: install test fetch compare run holdout walkforward gui docker-build docker-test docker-gui

install:
	pip install -e ".[dev]"

test:
	pytest -q

# 認証不要。仕組みの確認用（成績に意味はない）
compare:
	python scripts/run.py --compare --synthetic --random-runs 40

# 実データの取り込み（APIキーが必要。置き場所は docs/secrets.md）
fetch:
	python scripts/fetch.py --years 2

run:
	python scripts/run.py --strategy sma_cross --costs all

# Out-of-Sample 検証（★戦略を信じる前にこれ）
holdout:
	python scripts/validate.py holdout --strategy sma_cross --synthetic

walkforward:
	python scripts/validate.py walkforward --strategy sma_cross --synthetic --train 250 --test 120

# 運用の操作盤（http://127.0.0.1:8501）
gui:
	python scripts/gui.py

# --- Docker ---------------------------------------------------------------
# 相場データと台帳はイメージに入れず、このフォルダの data/ と state/ をつなぐ。
# GUI はこのMacからだけ接続できるよう、127.0.0.1 に限って公開する
IMAGE = algo-trading-system

docker-build:
	docker build --build-arg JQBT_VERSION=$$(git rev-parse --short HEAD)$$(git diff --quiet -- src scripts || echo +未コミットの変更あり) -t $(IMAGE) .

docker-test:
	docker run --rm $(IMAGE)

docker-gui:
	docker run --rm -p 127.0.0.1:8501:8501 -v "$(PWD)/data:/app/data:ro" -v "$(PWD)/state:/app/state" \
		-e JQBT_EXTENSIONS -e AI_MONTHLY_BUDGET_USD -e AI_MODEL -e AI_VERTEX_REGION -e GCP_PROJECT $(IMAGE) python scripts/gui.py
