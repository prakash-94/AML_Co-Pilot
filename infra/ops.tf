# Scheduled quality check (the "canary") and the alerts that make failures visible.

resource "google_service_account" "eval" {
  account_id   = "aml-eval"
  display_name = "AML Copilot scheduled evaluation"
}

resource "google_service_account" "scheduler" {
  account_id   = "aml-scheduler"
  display_name = "Cloud Scheduler caller of the evaluation job"
}

resource "google_project_iam_member" "eval_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = google_service_account.eval.member
}

resource "google_secret_manager_secret_iam_member" "eval_secrets" {
  for_each  = google_secret_manager_secret.secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.eval.member
}

# A Cloud Run *Job*: runs to completion, then stops billing. Exit code 1 (a regression) marks the execution failed.
resource "google_cloud_run_v2_job" "eval" {
  count               = var.deploy_services ? 1 : 0
  name                = "aml-eval"
  location            = var.region
  deletion_protection = false

  # The image is rolled out by the deploy workflow / scripts, so Terraform must not revert it.
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image]
  }

  template {
    template {
      service_account = google_service_account.eval.email
      timeout         = "3600s"
      max_retries     = 0 # a retry would be a second paid run
      containers {
        image   = local.image
        command = ["python", "-m", "aml_copilot.evaluation"]
        args    = ["canary", "--yes", "--sink", "delta", "--cap", tostring(var.eval_cap_usd)]
        resources {
          limits = { cpu = "2", memory = "4Gi" }
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
  }
  depends_on = [google_secret_manager_secret_iam_member.eval_secrets]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_eval" {
  count    = var.deploy_services ? 1 : 0
  name     = google_cloud_run_v2_job.eval[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler.member
}

resource "google_cloud_scheduler_job" "eval_weekly" {
  count     = var.deploy_services ? 1 : 0
  name      = "aml-eval-weekly"
  region    = var.region
  schedule  = var.eval_schedule
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/aml-eval:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }
}

# ---------------------------------------------------------------------------- alerts
resource "google_monitoring_notification_channel" "email" {
  display_name = "AML Copilot owner"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

resource "google_monitoring_alert_policy" "server_errors" {
  display_name = "AML Copilot: server errors (5xx)"
  combiner     = "OR"
  conditions {
    display_name = "5xx responses from the API or worker"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 2
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
}

resource "google_monitoring_alert_policy" "queue_backlog" {
  display_name = "AML Copilot: investigations waiting too long"
  combiner     = "OR"
  conditions {
    display_name = "Cloud Tasks queue depth above 5 for 10 minutes"
    condition_threshold {
      filter          = "resource.type = \"cloud_tasks_queue\" AND metric.type = \"cloudtasks.googleapis.com/queue/depth\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
}

resource "google_logging_metric" "job_crashes" {
  name   = "aml_job_crashes"
  filter = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"aml-worker\" AND (textPayload:\"job_crashed\" OR jsonPayload.message:\"job_crashed\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "job_crashes" {
  display_name = "AML Copilot: an investigation crashed"
  combiner     = "OR"
  conditions {
    display_name = "job_crashed logged by the worker"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.job_crashes.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
}

resource "google_monitoring_alert_policy" "canary_failed" {
  display_name = "AML Copilot: scheduled quality check failed"
  combiner     = "OR"
  conditions {
    display_name = "aml-eval execution failed (a regression or an outage)"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND resource.labels.job_name = \"aml-eval\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
}
