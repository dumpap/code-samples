variable "project_id" {
  type        = string
  description = "Project ID where the function and bucket will live"
}

variable "region" {
  type        = string
  description = "Region for Cloud Function and buckets"
  default     = "australia-southeast1"
}

variable "org_id" {
  type        = string
  description = "Organization numeric ID"
}

variable "bucket_name" {
  type        = string
  description = "Optional custom name for the report bucket"
  default     = null
}

variable "allowed_au_locations" {
  type        = string
  description = "CSV of allowed AU locations"
  default     = "australia-southeast1,australia-southeast2"
}

variable "enable_recommender" {
  type        = bool
  description = "Whether to fetch Active Assist recommendations"
  default     = true
}

variable "manage_org_iam" {
  type        = bool
  description = "Whether this module should bind org-level IAM to the function SA"
  default     = true
}

variable "ingress_setting" {
  type        = string
  description = "Ingress settings for the function (ALLOW_INTERNAL_ONLY, ALLOW_INTERNAL_AND_GCLB, ALLOW_ALL)"
  default     = "ALLOW_INTERNAL_AND_GCLB"
}

variable "http_timeout_seconds" {
  type        = number
  description = "HTTP timeout per API call"
  default     = 60
}

variable "function_source_dir" {
  type        = string
  description = "Local path to the Cloud Function source directory"
  default     = "../cloud_function"
}

variable "scheduler_cron" {
  type        = string
  description = "Cron schedule for Cloud Scheduler"
  default     = "0 1 * * *"
}

variable "scheduler_time_zone" {
  type        = string
  description = "Timezone for Scheduler"
  default     = "Etc/UTC"
}
