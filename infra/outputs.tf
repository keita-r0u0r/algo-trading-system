output "url" {
  description = "画面のアドレス（allowed_users のアカウントでログインして開く）"
  value       = length(google_cloud_run_v2_service.gui) == 0 ? "" : google_cloud_run_v2_service.gui[0].uri
}

output "image" {
  value = local.image
}

output "state_bucket" {
  value = google_storage_bucket.state.name
}

output "build_source_bucket" {
  value = google_storage_bucket.build_source.name
}

output "builder_service_account" {
  value = google_service_account.builder.id
}

# 次に make infra-apply / infra-plan したときに、動いている版をそのまま使うため
output "image_tag" {
  value = var.image_tag
}

output "app_version" {
  value = var.app_version
}

output "egress_ip" {
  description = "自動売買の出口の固定 IP（GMOコインの注文用のキーの IP 制限に登録する）"
  value       = length(google_compute_address.egress) == 0 ? "" : google_compute_address.egress[0].address
}

output "watcher_ip" {
  description = "見張り役（随時判断・発注）の固定 IP。GMOコインの注文用のキーの IP 制限に足す"
  value       = length(google_compute_address.watcher) == 0 ? "" : google_compute_address.watcher[0].address
}
