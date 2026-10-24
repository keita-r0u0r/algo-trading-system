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
  services = ["run", "artifactregistry", "cloudbuild", "iap", "aiplatform", "secretmanager", "storage", "cloudscheduler"]
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
      keep_count = 5
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
          memory = "2Gi"
        }
        # 画面を開いていない間も、台帳の複製を止めない
        cpu_idle          = false
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
      # やり直すと同じ1時間に二重に判断するので、やり直さない（次の1時間に任せる）
      max_retries = 0

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
