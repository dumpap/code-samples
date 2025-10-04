output "function_uri" {
  value       = google_cloudfunctions2_function.report_fn.service_config[0].uri
  description = "HTTP URL of the Cloud Function"
}

output "report_bucket" {
  value       = google_storage_bucket.report_bucket.name
  description = "Bucket where HTML reports are stored"
}

output "service_account_email" {
  value       = google_service_account.report_sa.email
  description = "Service account used by the Cloud Function"
}
