variable "project_id" {
  type        = string
  description = "Google Cloud project id"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag (use the git sha) of the image in Artifact Registry"
}

variable "deploy_services" {
  type        = bool
  default     = false
  description = "False on the first apply (creates the registry etc.); true once the image has been pushed"
}

variable "llm_provider" {
  type    = string
  default = "vertex"
}

variable "evidence_model" {
  type    = string
  default = "claude-haiku-4-5"
}

variable "judge_model" {
  type    = string
  default = "claude-sonnet-5"
}

variable "daily_investigation_limit" {
  type    = number
  default = 30
}

variable "audit_retention_seconds" {
  type    = number
  default = 604800 # 7 days
}

# Non-secret connection settings (the secret ones live in Secret Manager)
variable "databricks_server_hostname" {
  type    = string
  default = ""
}

variable "databricks_http_path" {
  type    = string
  default = ""
}

variable "databricks_transactions_table" {
  type    = string
  default = "aml_copilot.transactions"
}

variable "sf_username" {
  type    = string
  default = ""
}

variable "databricks_client_id" {
  type        = string
  default     = ""
  description = "Application id of the Databricks service principal (not secret); its secret is in Secret Manager"
}

variable "alert_email" {
  type        = string
  default     = ""
  description = "Where alerts are emailed"
}

variable "eval_cap_usd" {
  type        = number
  default     = 0.6
  description = "Hard spending cap for each scheduled quality check"
}

variable "eval_schedule" {
  type        = string
  default     = "0 6 * * 1" # Mondays 06:00 UTC
  description = "Cron schedule of the quality check"
}
