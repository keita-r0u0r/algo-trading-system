# 仮想環境（.venv）があればその Python を使う
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: install test fetch compare run holdout walkforward gui docker-build docker-test docker-gui infra-init infra-plan infra-apply image deploy cloud-seed paper

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

# 認証不要。仕組みの確認用（成績に意味はない）
compare:
	$(PY) scripts/run.py --compare --synthetic --random-runs 40

# 実データの取り込み（APIキーが必要。置き場所は docs/secrets.md）
fetch:
	$(PY) scripts/fetch.py --years 2

run:
	$(PY) scripts/run.py --strategy sma_cross --costs all

# Out-of-Sample 検証（★戦略を信じる前にこれ）
holdout:
	$(PY) scripts/validate.py holdout --strategy sma_cross --synthetic

walkforward:
	$(PY) scripts/validate.py walkforward --strategy sma_cross --synthetic --train 250 --test 120

# 運用の操作盤（http://127.0.0.1:8501）
gui:
	$(PY) scripts/gui.py

# FX のペーパー運用を1回進める（Cloud Run では1時間に1回、自動。注文は出さない）
paper:
	$(PY) scripts/fx_paper.py

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

# --- GCP（Cloud Run）。手順は docs/cloud-run.md -----------------------------
# 作るものは infra/（OpenTofu）にすべて書いてある。手で作らない
PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)
TOFU = tofu -chdir=infra
VERSION = $$(git rev-parse --short HEAD)$$(git diff --quiet -- src scripts deploy Dockerfile || echo +未コミットの変更あり)

# 最初の1回: 状態（tfstate）の置き場所を作って、つなぐ
infra-init:
	gcloud storage buckets describe gs://$(PROJECT)-tfstate >/dev/null 2>&1 || \
		gcloud storage buckets create gs://$(PROJECT)-tfstate --location=asia-northeast1 \
			--uniform-bucket-level-access --public-access-prevention
	gcloud storage buckets update gs://$(PROJECT)-tfstate --versioning
	$(TOFU) init -backend-config=bucket=$(PROJECT)-tfstate

# 動いている版（前回の deploy）を引き継ぐ。渡さないと Cloud Run を消す計画になる
CURRENT = -var image_tag="$$($(TOFU) output -raw image_tag 2>/dev/null)" -var app_version="$$($(TOFU) output -raw app_version 2>/dev/null || echo unknown)"

infra-plan:
	$(TOFU) plan $(CURRENT)

# 権限やバケットなどの変更を反映する（版は変えない）。最初の1回は Cloud Run 以外を作る
infra-apply:
	$(TOFU) apply $(CURRENT)

# イメージを Cloud Build で作る
image:
	gcloud builds submit --project=$(PROJECT) --region=asia-northeast1 --config=deploy/cloudbuild.yaml \
		--service-account=$$($(TOFU) output -raw builder_service_account) \
		--gcs-source-staging-dir=gs://$$($(TOFU) output -raw build_source_bucket)/source \
		--substitutions=_IMAGE=$$($(TOFU) output -raw image),_TAG=$$(git rev-parse --short HEAD),_VERSION="$(VERSION)"

# イメージを作って、その版で Cloud Run を更新する
deploy: image
	$(TOFU) apply -var image_tag=$$(git rev-parse --short HEAD) -var app_version="$(VERSION)"

# 最初の1回: 手元の記録と相場データをバケットに上げる（台帳は state/seed/ に置き、初回の起動で使う）
cloud-seed:
	B=$$($(TOFU) output -raw state_bucket); \
	gcloud storage rsync --recursive data gs://$$B/data && \
	gcloud storage rsync --recursive --exclude='.*\.db$$' state gs://$$B/state && \
	gcloud storage cp state/*.db gs://$$B/state/seed/

