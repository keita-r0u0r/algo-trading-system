variable "project" {
  description = "GCP のプロジェクトID"
  type        = string
}

variable "region" {
  description = "Cloud Run・バケット・イメージの置き場所"
  type        = string
  default     = "asia-northeast1"
}

variable "allowed_users" {
  description = "画面を開けるアカウント（IAP）。例: [\"user:you@example.com\"]"
  type        = list(string)
}

variable "image_tag" {
  description = "動かすイメージのタグ（make deploy が Git の版を渡す）。空なら Cloud Run はまだ作らない"
  type        = string
  default     = ""
}

variable "app_version" {
  description = "画面に出す版（make deploy が渡す）"
  type        = string
  default     = "unknown"
}

variable "ai_monthly_budget_usd" {
  description = "AI の月の費用の上限（米ドル）"
  type        = number
  default     = 5
}

variable "fx_paper_risk" {
  description = "FX のペーパー運用のリスクの度合い（small / medium / max）"
  type        = string
  default     = "medium"
}

variable "fx_paper_paused" {
  description = "true にすると、1時間ごとの実行を止める"
  type        = bool
  default     = false
}

variable "iap_oauth_client_id" {
  description = "IAP のログインに使う OAuth クライアントID（組織に属していないプロジェクトで必要）。空なら設定しない"
  type        = string
  default     = ""
}

variable "fx_paper_schedule" {
  description = <<-EOT
    FX のペーパー運用を動かす時刻（cron、日本時間）。既定は月〜土の毎時1分（土曜の朝まで市場が開いている）。
    市場が閉まっていればスクリプトが何もしないので、広めに書いてよい。
    費用を抑えるなら平日の日中だけ（例: "1 9-18 * * 1-5"）
  EOT
  type        = string
  default     = "1 * * * 1-6"
}
