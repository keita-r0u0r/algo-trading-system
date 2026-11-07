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
    5分ごとのジョブ（見張り役の控え）を動かす時刻（cron、日本時間）。既定は市場が開いている間のうち、月〜金の 7:00〜23:55。
    残りの夜の分は fx_paper_schedule_night。費用を抑えるなら間隔を広げる（例: "*/15 7-23 * * 1-5"）
  EOT
  type        = string
  default     = "*/5 7-23 * * 1-5"
}

variable "fx_paper_schedule_night" {
  description = "同じジョブの、火〜土の 0:00〜6:55（cron、日本時間）。fx_paper_schedule と合わせて、月曜 7:00〜土曜 6:55 になる"
  type        = string
  default     = "*/5 0-6 * * 2-6"
}

variable "fx_live_egress" {
  description = "自動売買の出口の IP を固定する（Cloud NAT と固定 IP。有料）。注文用のキーの IP 制限に使う"
  type        = bool
  default     = false
}

variable "fx_live_enabled" {
  description = "自動売買（実際のお金）で注文を出す。false なら、何をするかを記録するだけ"
  type        = bool
  default     = false
}

variable "fx_watch_enabled" {
  description = "値段の見張り役（us-central1 の e2-micro。本体は無料の枠、外向きの IP は有料）を動かす"
  type        = bool
  default     = false
}
