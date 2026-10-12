"""Settings loaded from environment variables (or a local .env file)."""

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from aml_copilot.errors import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

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

    # Which model family answers: "gemini" (Google Gemini on Vertex AI - the deployed setup), "anthropic" (Claude through
    # the Anthropic API, needs ANTHROPIC_API_KEY) or "vertex" (Claude on Vertex AI, needs Claude quota).
    llm_provider: str = "gemini"
    gcp_project: str = ""
    # Gemini 3.x models and Claude Sonnet 5 are served on the "global" endpoint.
    vertex_location: str = "global"
    # How Google credentials are found: "adc" = Application Default Credentials (a laptop with gcloud, or Google's own
    # compute); "databricks" = keyless federation - the Databricks service principal's token is exchanged for a Google
    # identity (no key file is ever stored). The next two settings are only for "databricks".
    gcp_auth: str = "adc"
    # "token" = a short-lived access token from GCP_ACCESS_TOKEN (e.g. `gcloud auth print-access-token`), for quick local runs.
    gcp_access_token: SecretStr = SecretStr("")
    gcp_wif_audience: str = ""  # //iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>
    gcp_service_account: str = ""  # the account that holds the Vertex AI permission, e.g. aml-gemini@<project>.iam.gserviceaccount.com
    # Optional overrides of the logical-name -> Vertex id map for Claude on Vertex (agents/llm.py), as JSON.
    vertex_model_ids: dict[str, str] = Field(default_factory=dict)

    anthropic_api_key: SecretStr = SecretStr("")
    # The model used by any step that has no model of its own (the evidence summarizer). Reads LLM_MODEL, or the older
    # ANTHROPIC_MODEL.
    llm_model: str = Field(
        default="gemini-3.5-flash", validation_alias=AliasChoices("llm_model", "anthropic_model")
    )
    # Optional separate model for the typology judge; blank means "use llm_model".
    typology_model: str = ""
    # Likewise for the narrative writer.
    narrative_model: str = ""
    # Likewise for the faithfulness judge (the model that fact-checks the narrative).
    faithfulness_model: str = ""

    @property
    def anthropic_model(self) -> str:
        """Older name for `llm_model`."""
        return self.llm_model

    # How log lines look: "console" for a person at a terminal, "json" for Cloud Logging.
    log_format: str = "console"
    log_level: str = "INFO"

    # Recorded in each draft's provenance (set by the deploy; blank when running locally).
    build_sha: str = ""


def require(settings: Settings, *names: str) -> None:
    """Raise ConfigError naming every unset setting in `names`."""
    missing = [n.upper() for n in names if not getattr(settings, n)]
    if missing:
        raise ConfigError(f"Missing required settings: {', '.join(missing)}")
