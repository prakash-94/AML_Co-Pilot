# Lets GitHub Actions deploy new images WITHOUT a stored key: GitHub proves its identity to Google
# (Workload Identity Federation) and may act only as the deployer account, only from this repository.
# The deployer can build and roll out images; it cannot change infrastructure (Terraform stays manual).

variable "github_repo" {
  type        = string
  default     = ""
  description = "owner/name of the GitHub repository allowed to deploy, e.g. alice/aml-copilot. Empty = no CI access."
}

resource "google_service_account" "deployer" {
  count        = var.github_repo == "" ? 0 : 1
  account_id   = "aml-deployer"
  display_name = "GitHub Actions deployer"
}

resource "google_iam_workload_identity_pool" "github" {
  count                     = var.github_repo == "" ? 0 : 1
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count                              = var.github_repo == "" ? 0 : 1
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == \"${var.github_repo}\""
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_acts_as_deployer" {
  count              = var.github_repo == "" ? 0 : 1
  service_account_id = google_service_account.deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repo}"
}

resource "google_project_iam_member" "deployer_roles" {
  for_each = var.github_repo == "" ? toset([]) : toset([
    "roles/cloudbuild.builds.editor",
    "roles/run.developer",
    "roles/artifactregistry.writer",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/logging.viewer",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.deployer[0].member
}

# Uploading build sources: object access on the Cloud Build bucket only (not the state or audit buckets).
resource "google_storage_bucket_iam_member" "deployer_build_bucket" {
  count  = var.github_repo == "" ? 0 : 1
  bucket = "${var.project_id}_cloudbuild"
  role   = "roles/storage.objectAdmin"
  member = google_service_account.deployer[0].member
}

# Cloud Run only lets a caller deploy a service if the caller may act as that service's runtime account.
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  for_each = var.github_repo == "" ? {} : {
    api    = google_service_account.api.name
    worker = google_service_account.worker.name
    eval   = google_service_account.eval.name
  }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer[0].member
}

output "github_workload_identity_provider" {
  value = var.github_repo == "" ? null : google_iam_workload_identity_pool_provider.github[0].name
}

output "github_deployer_service_account" {
  value = var.github_repo == "" ? null : google_service_account.deployer[0].email
}
