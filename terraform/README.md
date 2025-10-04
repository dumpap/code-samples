# GCP Org Security Report - Cloud Function + Scheduler

## What this deploys
- Service account for the function
- Storage bucket for HTML reports
- Cloud Function (2nd gen, Python 3.11, HTTP)
- Cloud Scheduler job (OIDC) to invoke the function

## Required inputs
- `var.project_id`: Project to host function and buckets
- `var.region`: Region (default `australia-southeast1`)
- `var.org_id`: Organization numeric ID

## Minimal IAM
- Function SA gets:
  - `roles/browser` on org
  - `roles/cloudasset.viewer` on org
  - `roles/recommender.viewer` on org (optional, with `var.enable_recommender`)
- Function SA gets `roles/storage.objectCreator` on the report bucket
- Scheduler SA gets `roles/run.invoker` on the function

## Prereqs
Enable APIs on `var.project_id`:
- `cloudfunctions.googleapis.com`
- `run.googleapis.com`
- `cloudbuild.googleapis.com`
- `artifactregistry.googleapis.com`
- `cloudscheduler.googleapis.com`
- `cloudasset.googleapis.com`
- `recommender.googleapis.com` (if enabled)
- `storage.googleapis.com`

## Deploy
```bash
cd terraform
terraform init
terraform apply \
  -var project_id=YOUR_PROJECT \
  -var org_id=YOUR_ORG_ID \
  -var region=australia-southeast1
```

## Configure
- The Function uses its default credential; the `x-goog-user-project` header is set to `var.project_id` automatically.
- Reports are uploaded to `report_bucket` output, files named `gcp_org_security_report_YYYYMMDD_HHMMSSZ.html`.

## Trigger
- Scheduler triggers daily by default at 01:00 UTC (`var.scheduler_cron`, `var.scheduler_time_zone`).
