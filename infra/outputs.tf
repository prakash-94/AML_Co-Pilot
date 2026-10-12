output "image" {
  value = local.image
}

output "worker_url" {
  value = local.worker_url
}

output "api_url" {
  value = var.deploy_services ? google_cloud_run_v2_service.api[0].uri : null
}

output "audit_bucket" {
  value = google_storage_bucket.audit.name
}
