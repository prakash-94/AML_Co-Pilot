"""Settings loaded from environment variables (or a local .env file)."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from aml_copilot.errors import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    databricks_server_hostname: str = ""
    databricks_http_path: str = ""
    databricks_token: SecretStr = SecretStr("")
    # Preferred over the token: a service principal (OAuth machine-to-machine). When both are set, OAuth wins.
    databricks_client_id: str = ""
    databricks_client_secret: SecretStr = SecretStr("")
    databricks_transactions_table: str = "aml_copilot.transactions"
    transaction_row_limit: int = 10_000

    sf_username: str = ""
    sf_password: SecretStr = SecretStr("")
    sf_security_token: SecretStr = SecretStr("")

    supabase_db_url: SecretStr = SecretStr("")

    # Where Claude is called from: "anthropic" (the Anthropic API, needs ANTHROPIC_API_KEY) or
    # "vertex" (Claude on Google Cloud Vertex AI, uses the service's own Google identity - no key).
    llm_provider: str = "anthropic"
    gcp_project: str = ""
    # Claude Sonnet 5 is only served on the "global" and US/EU multi-region endpoints.
    vertex_location: str = "global"
    # Optional overrides of the logical-name -> Vertex id map in agents/llm.py, as JSON, e.g.
    # VERTEX_MODEL_IDS='{"claude-haiku-4-5": "claude-haiku-4-5@20251001"}'
    vertex_model_ids: dict[str, str] = Field(default_factory=dict)

    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-sonnet-5"
    # Optional separate model for the typology judge; blank means "use anthropic_model".
    typology_model: str = ""
    # Likewise for the narrative writer.
    narrative_model: str = ""
    # Likewise for the faithfulness judge (the model that fact-checks the narrative).
    faithfulness_model: str = ""

    # How log lines look: "console" for a person at a terminal, "json" for Cloud Logging.
    log_format: str = "console"
    log_level: str = "INFO"

    # --- The deployed service (aml_copilot/service) ---
    service_role: str = "api"  # "api" serves the browser; "worker" runs investigations
    service_backend: str = "memory"  # "memory" for local development; "gcp" = Firestore + Cloud Storage + Cloud Tasks
    auth_mode: str = "dev"  # "dev" (fixed test tokens, refused on Cloud Run) or "firebase"
    firebase_project_id: str = ""
    audit_bucket: str = ""  # Cloud Storage bucket holding the audit trail
    tasks_location: str = "us-central1"
    tasks_queue: str = "investigations"
    worker_url: str = ""  # the worker's https address; also the audience of Cloud Tasks' token
    tasks_invoker_email: str = ""  # the service account Cloud Tasks signs its calls as
    daily_investigation_limit: int = 30
    api_prefix: str = ""  # "/api" when served behind Firebase Hosting (which forwards the full path)
    build_sha: str = ""


def require(settings: Settings, *names: str) -> None:
    """Raise ConfigError naming every unset setting in `names`."""
    missing = [n.upper() for n in names if not getattr(settings, n)]
    if missing:
        raise ConfigError(f"Missing required settings: {', '.join(missing)}")
