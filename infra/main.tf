# 運用の操作盤（GUI）を Cloud Run で動かすための一式。
#
#   画面を開けるのは allowed_users だけ（IAP。Google のログインで確かめる）
#   台帳・設定・相場データは、非公開のバケット（state_bucket）に置く
#   AI は Vertex AI を、このサービス専用のアカウントの権限で呼ぶ
#
# 注文を出す仕組みは無い。ここで作るのは、画面とその記録の置き場所だけ。

data "google_project" "this" {}

locals {
  name     = "algo-trading"
  services = ["run", "artifactregistry", "cloudbuild", "iap", "aiplatform", "secretmanager", "storage", "cloudscheduler", "compute"]
  image    = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.images.repository_id}/gui"
}

resource "google_project_service" "apis" {
  for_each           = toset(local.services)
  service            = "${each.key}.googleapis.com"
  disable_on_destroy = false
}

# --- 記録と相場データの置き場所 ---------------------------------------------

resource "google_storage_bucket" "state" {
  name                        = "${var.project}-${local.name}-state"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # 消したり上書きしたりした記録を30日は戻せるように
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 30
    }
    action {
      type = "Delete"
    }
  }

  # 運用の記録の正本。誤って消さない
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.apis]
}

# Cloud Build に送るソースの置き場所（make image）
resource "google_storage_bucket" "build_source" {
  name                        = "${var.project}-${local.name}-build"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

# --- イメージ ---------------------------------------------------------------

resource "google_artifact_registry_repository" "images" {
  repository_id = local.name
  location      = var.region
  format        = "DOCKER"

  # 置いておくのは最近の5つ（無料の枠 0.5GB に収める）
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      # 前の版に戻すときは、イメージが無ければソースから作り直す（make rollback）ので、少しだけ残す
      keep_count = 3
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "604800s"
    }
  }
  depends_on = [google_project_service.apis]
}

# --- サービスアカウント -------------------------------------------------------

# イメージを作る（Cloud Build）
resource "google_service_account" "builder" {
  account_id   = "${local.name}-builder"
  display_name = "algo-trading: イメージを作る"
}

resource "google_project_iam_member" "builder_logs" {
  project = var.project
  role    = "roles/logging.logWriter"
  member  = google_service_account.builder.member
}

resource "google_artifact_registry_repository_iam_member" "builder_push" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.builder.member
}

resource "google_storage_bucket_iam_member" "builder_source" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.builder.member
}

# GUI を動かす（Cloud Run）
resource "google_service_account" "gui" {
  account_id   = "${local.name}-gui"
  display_name = "algo-trading: 運用の操作盤"
}

resource "google_storage_bucket_iam_member" "gui_state" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectUser"
  member = google_service_account.gui.member
}

resource "google_project_iam_member" "gui_ai" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = google_service_account.gui.member
}

resource "google_project_iam_member" "gui_secrets" {
  project = var.project
  role    = "roles/secretmanager.secretAccessor"
  member  = google_service_account.gui.member
}

# --- 画面（Cloud Run） ---------------------------------------------------------

resource "google_cloud_run_v2_service" "gui" {
  count = var.image_tag == "" ? 0 : 1

  name                = "${local.name}-gui"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false

  template {
    service_account       = google_service_account.gui.email
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
    # Streamlit は1つの接続（WebSocket）を開いたまま使う
    timeout          = "3600s"
    session_affinity = true

    # 台帳を書くのは1台だけ（2台で書くと複製が食い違う）
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image   = "${local.image}:${var.image_tag}"
      command = ["/app/deploy/entrypoint.sh"]

      ports {
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        # 画面を使っている（要求を処理している）間だけ CPU を割り当てて課金する。国内株の台帳の複製は、国内株を
        # 動かしていない（準備中）ので止まってよい。常時割り当て（false）だと、開いたままの画面で一日中課金される
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "JQBT_VERSION"
        value = var.app_version
      }
      env {
        name  = "JQBT_EXTENSIONS"
        value = "ai"
      }
      env {
        name  = "JQBT_LEDGER_DIR"
        value = "/var/ledger"
      }
      env {
        name  = "STATE_BUCKET"
        value = google_storage_bucket.state.name
      }
      # 画面に「注文を出す設定か」を出すため（画面からは注文しない）
      env {
        name  = "FX_LIVE_ENABLED"
        value = tostring(var.fx_live_enabled)
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project
      }
      env {
        name  = "AI_VERTEX_REGION"
        value = "global"
      }
      env {
        name  = "AI_MONTHLY_BUDGET_USD"
        value = tostring(var.ai_monthly_budget_usd)
      }

      volume_mounts {
        name       = "state"
        mount_path = "/app/state"
      }
      volume_mounts {
        name       = "data"
        mount_path = "/app/data"
      }

      startup_probe {
        http_get {
          path = "/_stcore/health"
        }
        period_seconds    = 5
        failure_threshold = 36
      }
    }

    volumes {
      name = "state"
      gcs {
        bucket        = google_storage_bucket.state.name
        mount_options = ["only-dir=state", "implicit-dirs", "uid=1000", "gid=1000"]
      }
    }
    volumes {
      name = "data"
      gcs {
        bucket        = google_storage_bucket.state.name
        read_only     = true
        mount_options = ["only-dir=data", "implicit-dirs", "uid=1000", "gid=1000"]
      }
    }
  }

  depends_on = [
    google_storage_bucket_iam_member.gui_state,
    google_project_iam_member.gui_ai,
    google_project_iam_member.gui_secrets,
  ]
}

# IAP が Cloud Run を呼べるようにする
resource "google_project_service_identity" "iap" {
  provider = google-beta
  service  = "iap.googleapis.com"
}

resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  count    = length(google_cloud_run_v2_service.gui)
  name     = google_cloud_run_v2_service.gui[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}

# 画面を開けるアカウント
resource "google_iap_web_cloud_run_service_iam_member" "users" {
  for_each               = length(google_cloud_run_v2_service.gui) == 0 ? toset([]) : toset(var.allowed_users)
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.gui[0].name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.key
}

# --- FX のペーパー運用（1時間に1回、仮想の約定を進める） ------------------------------
# 注文は出さない。判断と仮想の約定を state/fx_paper.jsonl に追記するだけ（画面の「FX ペーパー」で見る）

resource "google_cloud_run_v2_job" "fx_paper" {
  count = var.image_tag == "" ? 0 : 1

  name                = "${local.name}-fx-paper"
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account       = google_service_account.gui.email
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
      timeout               = "600s"
      # やり直すと同じ回に二重に判断するので、やり直さない（次の回に任せる）
      max_retries = 0

      # 自動売買の出口: 外に出るときの IP を固定する（注文用のキーをこの IP だけに制限するため）
      dynamic "vpc_access" {
        for_each = var.fx_live_egress ? [1] : []
        content {
          egress = "ALL_TRAFFIC"
          network_interfaces {
            network    = google_compute_network.egress[0].id
            subnetwork = google_compute_subnetwork.egress[0].id
          }
        }
      }

      containers {
        image   = "${local.image}:${var.image_tag}"
        command = ["python", "scripts/fx_paper.py"]

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }
        env {
          name  = "JQBT_VERSION"
          value = var.app_version
        }
        env {
          name  = "JQBT_EXTENSIONS"
          value = "ai"
        }
        env {
          name  = "GCP_PROJECT"
          value = var.project
        }
        env {
          name  = "AI_VERTEX_REGION"
          value = "global"
        }
        env {
          name  = "AI_MONTHLY_BUDGET_USD"
          value = tostring(var.ai_monthly_budget_usd)
        }
        env {
          name  = "FX_PAPER_RISK"
          value = var.fx_paper_risk
        }
        env {
          name  = "FX_LIVE_ENABLED"
          value = tostring(var.fx_live_enabled)
        }
        volume_mounts {
          name       = "state"
          mount_path = "/app/state"
        }
      }

      volumes {
        name = "state"
        gcs {
          bucket        = google_storage_bucket.state.name
          mount_options = ["only-dir=state", "implicit-dirs", "uid=1000", "gid=1000"]
        }
      }
    }
  }

  depends_on = [
    google_storage_bucket_iam_member.gui_state,
    google_project_iam_member.gui_ai,
  ]
}

# ジョブを起こすだけのアカウント
resource "google_service_account" "scheduler" {
  account_id   = "${local.name}-scheduler"
  display_name = "algo-trading: ペーパー運用を1時間ごとに起こす"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  count    = length(google_cloud_run_v2_job.fx_paper)
  name     = google_cloud_run_v2_job.fx_paper[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler.member
}

resource "google_cloud_scheduler_job" "fx_paper" {
  count = length(google_cloud_run_v2_job.fx_paper)

  name      = "${local.name}-fx-paper-hourly"
  region    = var.region
  schedule  = var.fx_paper_schedule
  time_zone = "Asia/Tokyo"
  paused    = var.fx_paper_paused

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project}/locations/${var.region}/jobs/${google_cloud_run_v2_job.fx_paper[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_project_service.apis]
}

# cron は1本では「月曜 7:00〜土曜 7:00」を書けないので、日中と夜の2本に分ける
resource "google_cloud_scheduler_job" "fx_paper_night" {
  count = length(google_cloud_run_v2_job.fx_paper)

  name      = "${local.name}-fx-paper-night"
  region    = var.region
  schedule  = var.fx_paper_schedule_night
  time_zone = "Asia/Tokyo"
  paused    = var.fx_paper_paused

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project}/locations/${var.region}/jobs/${google_cloud_run_v2_job.fx_paper[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_project_service.apis]
}

# --- IAP のログイン（OAuth クライアント） --------------------------------------------
# 組織に属していないプロジェクトでは、Google が用意する OAuth クライアントが使えない
# （画面を開くと「Empty Google Account OAuth client ID(s)/secret(s)」）。自分で作ったものを使う。
# クライアントID は terraform.tfvars に、シークレットは Secret Manager（iap-oauth-client-secret）に置く。
# 手順は docs/cloud-run.md。シークレットは OpenTofu の状態（非公開のバケット）にも残る

data "google_secret_manager_secret_version" "iap_oauth" {
  count  = var.iap_oauth_client_id == "" ? 0 : 1
  secret = "iap-oauth-client-secret"
}

resource "google_iap_settings" "oauth" {
  count = var.iap_oauth_client_id == "" ? 0 : 1
  name  = "projects/${var.project}/iap_web"

  access_settings {
    oauth_settings {
      client_id     = var.iap_oauth_client_id
      client_secret = data.google_secret_manager_secret_version.iap_oauth[0].secret_data
    }
  }
  depends_on = [google_project_service.apis]
}

# --- 自動売買の出口（固定 IP） --------------------------------------------------------
# GMOコインの注文用のキーは、この IP だけから使えるようにする。fx_live_egress = true のときだけ作る（固定 IP は有料）

resource "google_compute_network" "egress" {
  count                   = var.fx_live_egress ? 1 : 0
  name                    = "${local.name}-egress"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "egress" {
  count         = var.fx_live_egress ? 1 : 0
  name          = "${local.name}-egress"
  region        = var.region
  network       = google_compute_network.egress[0].id
  ip_cidr_range = "10.8.0.0/26"
}

resource "google_compute_address" "egress" {
  count  = var.fx_live_egress ? 1 : 0
  name   = "${local.name}-egress"
  region = var.region
}

resource "google_compute_router" "egress" {
  count   = var.fx_live_egress ? 1 : 0
  name    = "${local.name}-egress"
  region  = var.region
  network = google_compute_network.egress[0].id
}

resource "google_compute_router_nat" "egress" {
  count                              = var.fx_live_egress ? 1 : 0
  name                               = "${local.name}-egress"
  router                             = google_compute_router.egress[0].name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.egress[0].self_link]
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# --- 見張り役（常時動く小さな仮想マシン） ----------------------------------------------
# GMOコインの値段の配信を受け取り続け、実際の売買の随時判断（急変など）と定期判断（5分ごと）を、この中で行う。
# 無料の枠がある us-central1 の e2-micro。発注するので、外に出る IP を固定し（watcher_ip）、注文用のキーに登録する

resource "google_service_account" "watcher" {
  count        = var.fx_watch_enabled ? 1 : 0
  account_id   = "${local.name}-watcher"
  display_name = "algo-trading: 値段の見張り役"
}

resource "google_cloud_run_v2_job_iam_member" "watcher_runs_job" {
  # 「起こした理由」をジョブに渡す（実行時の上書き）ため、invoker ではなく developer
  count    = var.fx_watch_enabled && length(google_cloud_run_v2_job.fx_paper) > 0 ? 1 : 0
  name     = google_cloud_run_v2_job.fx_paper[0].name
  location = var.region
  role     = "roles/run.developer"
  member   = google_service_account.watcher[0].member
}

resource "google_storage_bucket_iam_member" "watcher_state" {
  count  = var.fx_watch_enabled ? 1 : 0
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectUser"
  member = google_service_account.watcher[0].member
}

resource "google_artifact_registry_repository_iam_member" "watcher_pull" {
  count      = var.fx_watch_enabled ? 1 : 0
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.watcher[0].member
}

resource "google_project_iam_member" "watcher_secrets" {
  # 注文用・照会用のキーを読む（Secret Manager）
  count   = var.fx_watch_enabled ? 1 : 0
  project = var.project
  role    = "roles/secretmanager.secretAccessor"
  member  = google_service_account.watcher[0].member
}

resource "google_project_iam_member" "watcher_ai" {
  count   = var.fx_watch_enabled ? 1 : 0
  project = var.project
  role    = "roles/aiplatform.user"
  member  = google_service_account.watcher[0].member
}

resource "google_compute_address" "watcher" {
  count  = var.fx_watch_enabled ? 1 : 0
  name   = "${local.name}-watcher"
  region = "us-central1"
}

resource "google_project_iam_member" "watcher_logs" {
  count   = var.fx_watch_enabled ? 1 : 0
  project = var.project
  role    = "roles/logging.logWriter"
  member  = google_service_account.watcher[0].member
}

resource "google_compute_network" "watch" {
  count                   = var.fx_watch_enabled ? 1 : 0
  name                    = "${local.name}-watch"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "watch" {
  count         = var.fx_watch_enabled ? 1 : 0
  name          = "${local.name}-watch"
  region        = "us-central1"
  network       = google_compute_network.watch[0].id
  ip_cidr_range = "10.9.0.0/28"
}

# イメージの版が変わったら、仮想マシンを作り直して新しい版で動かす
resource "google_compute_instance" "watcher" {
  count        = var.fx_watch_enabled && var.image_tag != "" ? 1 : 0
  name         = "${local.name}-watcher"
  zone         = "us-central1-a"
  machine_type = "e2-micro"

  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
      size  = 10
      type  = "pd-standard"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.watch[0].id
    access_config {
      nat_ip = google_compute_address.watcher[0].address # 注文用のキーの IP 制限に登録する
    }
  }

  service_account {
    email  = google_service_account.watcher[0].email
    scopes = ["cloud-platform"]
  }

  # 反映のたびに仮想マシンを作り直すと、起動とイメージの取り込みで数分止まる。そこで、版（image）と環境変数（watch-env）を
  # メタデータに置き、仮想マシンの中の見張り（fx-watch-update、30秒ごと）が変化に気づいたら、新しいイメージを先に取ってから
  # コンテナだけを入れ替える（止まるのは十数秒）。メタデータの変更は、仮想マシンを止めずにその場で反映される。
  # 古いイメージは、取る前に消す（動いているものは消えない）。消さないと、反映を重ねるうちにディスクがいっぱいになって取れなくなる。
  # user-data（起動の手順）を変えたときだけは、反映で -replace=google_compute_instance.watcher[0] を付けて作り直す
  metadata = {
    google-logging-enabled = "true"
    image                  = "${local.image}:${var.image_tag}"
    watch-env              = <<-EOT
      GCP_PROJECT=${var.project}
      STATE_BUCKET=${google_storage_bucket.state.name}
      FX_JOB_NAME=${local.name}-fx-paper
      FX_JOB_REGION=${var.region}
      FX_WATCH_TRADES=true
      FX_LIVE_ENABLED=${var.fx_live_enabled}
      JQBT_EXTENSIONS=ai
      AI_VERTEX_REGION=global
      AI_MONTHLY_BUDGET_USD=${var.ai_monthly_budget_usd}
      JQBT_VERSION=${var.app_version}
    EOT
    user-data              = <<-EOT
      #cloud-config
      write_files:
      - path: /etc/systemd/system/fx-watch.service
        permissions: "0644"
        content: |
          [Unit]
          Description=FX price watcher
          Wants=network-online.target
          After=network-online.target
          [Service]
          Environment="HOME=/home/watcher"
          ExecStartPre=/bin/mkdir -p /home/watcher
          ExecStartPre=/usr/bin/docker-credential-gcr configure-docker --registries ${var.region}-docker.pkg.dev
          ExecStartPre=-/usr/bin/docker rm -f fx-watch
          ExecStartPre=/bin/bash -c 'M=http://metadata.google.internal/computeMetadata/v1/instance/attributes; curl -sf -H "Metadata-Flavor: Google" -o /home/watcher/image $$M/image && curl -sf -H "Metadata-Flavor: Google" -o /home/watcher/env $$M/watch-env && cat /home/watcher/image /home/watcher/env | md5sum > /home/watcher/applied'
          ExecStart=/bin/bash -c 'exec /usr/bin/docker run --rm --name=fx-watch --log-driver=gcplogs --env-file /home/watcher/env $$(cat /home/watcher/image) python scripts/fx_watch.py'
          ExecStop=/usr/bin/docker stop fx-watch
          Restart=always
          RestartSec=15
          [Install]
          WantedBy=multi-user.target
      - path: /etc/systemd/system/fx-watch-update.service
        permissions: "0644"
        content: |
          [Unit]
          Description=Swap the FX watcher container when the image or env in metadata changes
          [Service]
          Type=oneshot
          Environment="HOME=/home/watcher"
          ExecStart=/bin/bash -c 'set -o pipefail; M=http://metadata.google.internal/computeMetadata/v1/instance/attributes; SIG=$$(curl -sf -H "Metadata-Flavor: Google" $$M/image $$M/watch-env | md5sum) || exit 0; [ "$$SIG" = "$$(cat /home/watcher/applied 2>/dev/null)" ] && exit 0; IMG=$$(curl -sf -H "Metadata-Flavor: Google" $$M/image) || exit 0; /usr/bin/docker-credential-gcr configure-docker --registries ${var.region}-docker.pkg.dev && { /usr/bin/docker image prune -af >/dev/null || true; } && /usr/bin/docker pull "$$IMG" && systemctl restart fx-watch.service'
      - path: /etc/systemd/system/fx-watch-update.timer
        permissions: "0644"
        content: |
          [Unit]
          Description=Check the FX watcher image every 30 seconds
          [Timer]
          OnBootSec=60
          OnUnitActiveSec=30
          AccuracySec=5
          [Install]
          WantedBy=timers.target
      runcmd:
      - systemctl daemon-reload
      - systemctl enable --now fx-watch.service
      - systemctl enable --now fx-watch-update.timer
    EOT
  }

  allow_stopping_for_update = true
  depends_on = [
    google_storage_bucket_iam_member.watcher_state,
    google_artifact_registry_repository_iam_member.watcher_pull,
    google_project_iam_member.watcher_secrets,
    google_project_iam_member.watcher_ai,
  ]
}
