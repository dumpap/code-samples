terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.30.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  bucket_name = var.bucket_name != null ? var.bucket_name : "${var.project_id}-gcp-org-report"
}

# Enable required services in the host project
resource "google_project_service" "services" {
  for_each = toset([
    "cloudfunctions.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudasset.googleapis.com",
    "storage.googleapis.com",
  ])
  project = var.project_id
  service = each.key
  disable_on_destroy = false
}

resource "google_project_service" "recommender" {
  count                   = var.enable_recommender ? 1 : 0
  project                 = var.project_id
  service                 = "recommender.googleapis.com"
  disable_on_destroy      = false
}

resource "google_service_account" "report_sa" {
  account_id   = "gcp-org-report-sa"
  display_name = "GCP Org Report SA"
}

# Minimal IAM on the function SA to read org data and write to GCS
# Note: org_id must be provided. Assign org-level viewer and cloud asset viewer.
resource "google_organization_iam_member" "org_viewer" {
  count  = var.manage_org_iam ? 1 : 0
  org_id = var.org_id
  role   = "roles/browser"
  member = "serviceAccount:${google_service_account.report_sa.email}"
}

resource "google_organization_iam_member" "org_asset_viewer" {
  count  = var.manage_org_iam ? 1 : 0
  org_id = var.org_id
  role   = "roles/cloudasset.viewer"
  member = "serviceAccount:${google_service_account.report_sa.email}"
}

# Recommender (optional). Toggle with var.enable_recommender.
resource "google_organization_iam_member" "org_recommender_viewer" {
  count  = var.manage_org_iam && var.enable_recommender ? 1 : 0
  org_id = var.org_id
  role   = "roles/recommender.viewer"
  member = "serviceAccount:${google_service_account.report_sa.email}"
}

# Storage bucket to store reports
resource "google_storage_bucket" "report_bucket" {
  name     = local.bucket_name
  location = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

# Allow function SA to write to the bucket
resource "google_storage_bucket_iam_member" "bucket_writer" {
  bucket = google_storage_bucket.report_bucket.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.report_sa.email}"
}

# Cloud Function (2nd gen) - HTTP triggered
resource "google_cloudfunctions2_function" "report_fn" {
  name        = "gcp-org-report-fn"
  location    = var.region
  description = "Generates GCP org HTML report and uploads to GCS"

  build_config {
    runtime     = "python311"
    entry_point = "generate_report"

    source {
      storage_source {
        bucket = google_storage_bucket.source_bucket.name
        object = google_storage_bucket_object.source_archive.name
      }
    }
  }

  service_config {
    available_memory    = "512M"
    timeout_seconds     = 540
    min_instance_count  = 0
    max_instance_count  = 3
    ingress_settings    = var.ingress_setting
    service_account_email = google_service_account.report_sa.email
    environment_variables = {
      ORG_ID                 = var.org_id
      BUCKET_NAME            = google_storage_bucket.report_bucket.name
      ALLOWED_AU_LOCATIONS   = var.allowed_au_locations
      RECOMMENDER_ENABLED    = var.enable_recommender ? "true" : "false"
      HTTP_TIMEOUT_SECONDS   = tostring(var.http_timeout_seconds)
    }
  }
}

# Artifacts bucket and object for function source
resource "google_storage_bucket" "source_bucket" {
  name     = "${var.project_id}-cf-source"
  location = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

data "archive_file" "source_zip" {
  type        = "zip"
  output_path = "${path.module}/build/function_src.zip"
  source_dir  = var.function_source_dir
}

resource "google_storage_bucket_object" "source_archive" {
  name   = "function_src.zip"
  bucket = google_storage_bucket.source_bucket.name
  source = data.archive_file.source_zip.output_path
  content_type = "application/zip"
}

# Scheduler -> HTTP call to function with OIDC token
resource "google_service_account" "scheduler_sa" {
  account_id   = "gcp-org-report-scheduler"
  display_name = "GCP Org Report Scheduler SA"
}

resource "google_cloud_scheduler_job" "daily_job" {
  name        = "gcp-org-report-daily"
  description = "Trigger org report function daily"
  schedule    = var.scheduler_cron
  time_zone   = var.scheduler_time_zone

  http_target {
    http_method = "GET"
    uri         = google_cloudfunctions2_function.report_fn.service_config[0].uri

    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
      audience              = google_cloudfunctions2_function.report_fn.service_config[0].uri
    }
  }
}

# IAM for scheduler SA to invoke the function
resource "google_cloud_run_service_iam_member" "scheduler_invoker" {
  location = google_cloudfunctions2_function.report_fn.location
  project  = var.project_id
  service  = google_cloudfunctions2_function.report_fn.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_sa.email}"
}
