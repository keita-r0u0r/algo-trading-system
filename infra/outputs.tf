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
