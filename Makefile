.PHONY: install test fetch compare run holdout walkforward gui

install:
	pip install -e ".[dev]"

test:
	pytest -q

# 認証不要。仕組みの確認用（成績に意味はない）
compare:
	python scripts/run.py --compare --synthetic --random-runs 40

# 実データの取り込み（JQUANTS_API_KEY が必要）
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
