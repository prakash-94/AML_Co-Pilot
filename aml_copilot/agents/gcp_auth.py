"""How the code proves to Google who it is, without a stored key.

Two modes (`GCP_AUTH`):

* `adc`        Application Default Credentials: your `gcloud` login on a laptop, or the identity of Google's own compute.
* `token`      A short-lived access token you pass in (`GCP_ACCESS_TOKEN`), for quick local runs.
* `gcloud`     Your own `gcloud auth print-access-token`, renewed automatically whenever it expires (long local runs).
* `databricks` Keyless federation for Databricks jobs. The job already holds an OAuth token for its service principal;
               Google's Security Token Service swaps that token for a short-lived Google token, and the Google
               service account `GCP_SERVICE_ACCOUNT` is impersonated for the Vertex AI call. Nothing long-lived is
               stored anywhere - the pool only trusts this workspace's tokens, and only for this one principal.
"""

from typing import Any

from aml_copilot.config import Settings, require
from aml_copilot.errors import ConfigError

STS_URL = "https://sts.googleapis.com/v1/token"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
AUTH_MODES = ("adc", "databricks", "token", "gcloud")


def impersonation_url(service_account: str) -> str:
    return f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:generateAccessToken"


def google_credentials(settings: Settings, token_supplier: Any = None, gcloud_token: Any = None) -> Any:
    """Credentials for Vertex AI, or None to let the Google libraries find Application Default Credentials.

    `token_supplier` (a callable returning the Databricks token) is injectable for tests."""
    if settings.gcp_auth == "adc":
        return None
    if settings.gcp_auth == "gcloud":
        return _gcloud_credentials(gcloud_token)
    if settings.gcp_auth == "token":
        require(settings, "gcp_access_token")
        from google.oauth2.credentials import Credentials

        return Credentials(settings.gcp_access_token.get_secret_value())
    if settings.gcp_auth != "databricks":
        raise ConfigError(f"GCP_AUTH must be one of {AUTH_MODES}, not {settings.gcp_auth!r}")

    require(settings, "gcp_wif_audience", "gcp_service_account", "databricks_server_hostname", "databricks_client_id")
    from google.auth import identity_pool

    if token_supplier is None:
        from aml_copilot.ingestion.databricks_auth import DatabricksOAuthTokenProvider

        require(settings, "databricks_client_secret")
        token_supplier = DatabricksOAuthTokenProvider(
            settings.databricks_server_hostname, settings.databricks_client_id,
            settings.databricks_client_secret.get_secret_value(),
        ).token

    class _DatabricksToken(identity_pool.SubjectTokenSupplier):
        def get_subject_token(self, context: Any, request: Any) -> str:
            return token_supplier()

    return identity_pool.Credentials(
        audience=settings.gcp_wif_audience,
        subject_token_type=JWT_TOKEN_TYPE,
        token_url=STS_URL,
        subject_token_supplier=_DatabricksToken(),
        service_account_impersonation_url=impersonation_url(settings.gcp_service_account),
        scopes=[CLOUD_PLATFORM_SCOPE],
    )


def _print_access_token() -> str:
    import shutil
    import subprocess

    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        raise ConfigError("GCP_AUTH=gcloud needs the gcloud command-line tool on PATH")
    done = subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ConfigError("gcloud could not give an access token - run `gcloud auth login` first")
    return done.stdout.strip()


def _gcloud_credentials(gcloud_token: Any = None) -> Any:
    from datetime import UTC, datetime, timedelta

    from google.oauth2.credentials import Credentials

    fetch = gcloud_token or _print_access_token

    def refresh(request: Any, scopes: Any) -> tuple[str, datetime]:
        # google-auth wants a naive UTC expiry; gcloud tokens last an hour, so renew a little early.
        return fetch(), (datetime.now(UTC) + timedelta(minutes=50)).replace(tzinfo=None)

    return Credentials(token=None, refresh_handler=refresh)
