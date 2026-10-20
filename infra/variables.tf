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
