terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # State lives in a private, versioned bucket (bucket name in backend.hcl: `terraform init -backend-config=backend.hcl`).
  # It contains no secret values: secrets are created empty here and filled by scripts/put_secrets.py.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {}

locals {
  # Cloud Run gives every service a predictable address, so the API can be told the worker's URL
  # (and the worker its own, for checking Cloud Tasks' token) without a circular reference.
  worker_url = "https://aml-worker-${data.google_project.this.number}.${var.region}.run.app"
  image      = "${var.region}-docker.pkg.dev/${var.project_id}/aml/aml-copilot:${var.image_tag}"

  secrets = ["DATABRICKS_TOKEN", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SUPABASE_DB_URL", "ANTHROPIC_API_KEY", "DATABRICKS_CLIENT_SECRET"]

  # Settings shared by both roles. Names match aml_copilot/config.py.
  common_env = {
    LLM_PROVIDER                  = var.llm_provider
    GCP_PROJECT                   = var.project_id
    VERTEX_LOCATION               = "global"
    ANTHROPIC_MODEL               = var.evidence_model
    TYPOLOGY_MODEL                = var.judge_model
    NARRATIVE_MODEL               = var.judge_model
    FAITHFULNESS_MODEL            = var.judge_model
    FIREBASE_PROJECT_ID           = var.project_id
    AUDIT_BUCKET                  = google_storage_bucket.audit.name
    TASKS_LOCATION                = var.region
    TASKS_QUEUE                   = google_cloud_tasks_queue.investigations.name
    WORKER_URL                    = local.worker_url
    TASKS_INVOKER_EMAIL           = google_service_account.tasks_invoker.email
    DAILY_INVESTIGATION_LIMIT     = tostring(var.daily_investigation_limit)
    DATABRICKS_SERVER_HOSTNAME    = var.databricks_server_hostname
    DATABRICKS_HTTP_PATH          = var.databricks_http_path
    DATABRICKS_TRANSACTIONS_TABLE = var.databricks_transactions_table
    SF_USERNAME                   = var.sf_username
  }
}

# ---------------------------------------------------------------------------- container registry
resource "google_artifact_registry_repository" "aml" {
  repository_id = "aml"
  format        = "DOCKER"
  location      = var.region
  description   = "AML Copilot images"

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-last-three"
    action = "KEEP"
    most_recent_versions {
      keep_count = 3
    }
  }
  cleanup_policies {
    id     = "delete-the-rest"
    action = "DELETE"
    condition {
      older_than = "86400s"
    }
  }
}

# ---------------------------------------------------------------------------- identities
resource "google_service_account" "api" {
  account_id   = "aml-api"
  display_name = "AML Copilot API"
}

resource "google_service_account" "worker" {
  account_id   = "aml-worker"
  display_name = "AML Copilot worker"
}

resource "google_service_account" "tasks_invoker" {
  account_id   = "aml-tasks-invoker"
  display_name = "Cloud Tasks caller of the worker"
}

# ---------------------------------------------------------------------------- data stores
resource "google_firestore_database" "jobs" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
}

resource "google_storage_bucket" "audit" {
  name                        = "${var.project_id}-aml-audit"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # Objects cannot be overwritten or deleted until they are this old. NOT locked: locking is
  # irreversible and would block tearing the project down. Lock it for a real deployment.
  retention_policy {
    retention_period = var.audit_retention_seconds
    is_locked        = false
  }
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(local.secrets)
  secret_id = each.key
  replication {
    auto {}
  }
}

# ---------------------------------------------------------------------------- queue
resource "google_cloud_tasks_queue" "investigations" {
  name     = "investigations"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 2
    max_dispatches_per_second = 1
  }
  retry_config {
    max_attempts = 1 # a retry would be a second paid run
  }
}

# ---------------------------------------------------------------------------- permissions
resource "google_project_iam_member" "firestore" {
  for_each = { api = google_service_account.api.member, worker = google_service_account.worker.member }
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = each.value
}

# create-only writers: they can add and read audit objects but cannot delete or overwrite them
resource "google_storage_bucket_iam_member" "audit_create" {
  for_each = { api = google_service_account.api.member, worker = google_service_account.worker.member }
  bucket   = google_storage_bucket.audit.name
  role     = "roles/storage.objectCreator"
  member   = each.value
}

resource "google_storage_bucket_iam_member" "audit_read" {
  for_each = { api = google_service_account.api.member, worker = google_service_account.worker.member }
  bucket   = google_storage_bucket.audit.name
  role     = "roles/storage.objectViewer"
  member   = each.value
}

resource "google_project_iam_member" "worker_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = google_service_account.worker.member
}

resource "google_secret_manager_secret_iam_member" "worker_secrets" {
  for_each  = google_secret_manager_secret.secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.worker.member
}

resource "google_cloud_tasks_queue_iam_member" "api_enqueue" {
  name     = google_cloud_tasks_queue.investigations.name
  location = var.region
  role     = "roles/cloudtasks.enqueuer"
  member   = google_service_account.api.member
}

# the API creates tasks that carry a token for the invoker account, so it must be allowed to act as it
resource "google_service_account_iam_member" "api_acts_as_invoker" {
  service_account_id = google_service_account.tasks_invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.api.member
}

# ---------------------------------------------------------------------------- services
resource "google_cloud_run_v2_service" "worker" {
  count               = var.deploy_services ? 1 : 0
  name                = "aml-worker"
  location            = var.region
  deletion_protection = false

  # The image is rolled out by the deploy workflow / scripts, so Terraform must not revert it.
  lifecycle {
    ignore_changes = [template[0].containers[0].image, scaling]
  }

  template {
    service_account                  = google_service_account.worker.email
    timeout                          = "1800s"
    max_instance_request_concurrency = 1
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = local.image
      resources {
        limits            = { cpu = "2", memory = "4Gi" }
        startup_cpu_boost = true
      }
      env {
        name  = "SERVICE_ROLE"
        value = "worker"
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = google_secret_manager_secret.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.worker_secrets]
}

resource "google_cloud_run_v2_service" "api" {
  count               = var.deploy_services ? 1 : 0
  name                = "aml-api"
  location            = var.region
  deletion_protection = false

  # The image is rolled out by the deploy workflow / scripts, so Terraform must not revert it.
  lifecycle {
    ignore_changes = [template[0].containers[0].image, scaling]
  }

  template {
    service_account = google_service_account.api.email
    timeout         = "60s"
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = local.image
      resources {
        limits            = { cpu = "1", memory = "1Gi" }
        startup_cpu_boost = true
      }
      env {
        name  = "SERVICE_ROLE"
        value = "api"
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}

# Only our Cloud Tasks identity may call the worker.
resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  count    = var.deploy_services ? 1 : 0
  name     = google_cloud_run_v2_service.worker[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.tasks_invoker.member
}

# The API must be publicly reachable (Firebase Hosting cannot present Google credentials); every route
# except /status checks the user's Firebase token in code.
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  count    = var.deploy_services ? 1 : 0
  name     = google_cloud_run_v2_service.api[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
