"""Gemini on Vertex AI: how the model is built, how the keyless Google login is assembled, how thinking tokens are priced."""

import sys
import types

import pytest

from aml_copilot.agents.gcp_auth import STS_URL, google_credentials, impersonation_url
from aml_copilot.agents.llm import (
    GEMINI_DEFAULT_MAX_TOKENS,
    GEMINI_THINKING_HEADROOM,
    make_chat_model,
)
from aml_copilot.config import Settings
from aml_copilot.errors import ConfigError
from aml_copilot.evaluation.cost import usage_cost

AUDIENCE = "//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/databricks/providers/databricks"
SERVICE_ACCOUNT = "aml-gemini@proj.iam.gserviceaccount.com"


def gemini_settings(**overrides):
    base = {"_env_file": None, "llm_provider": "gemini", "gcp_project": "proj"}
    return Settings(**{**base, **overrides})


@pytest.fixture
def fake_chat_vertex(monkeypatch):
    built = []

    class FakeChatVertexAI:
        def __init__(self, **kwargs):
            built.append(kwargs)

    module = types.ModuleType("langchain_google_vertexai")
    module.ChatVertexAI = FakeChatVertexAI
    monkeypatch.setitem(sys.modules, "langchain_google_vertexai", module)
    return built


# ---------------------------------------------------------------------------------- the model
def test_gemini_is_the_default_family_and_flash_the_default_model():
    s = Settings(_env_file=None)
    assert s.llm_provider == "gemini" and s.llm_model == "gemini-3.5-flash" and s.gcp_auth == "adc"


def test_the_gemini_model_is_built_for_the_project_without_a_temperature(fake_chat_vertex):
    make_chat_model(gemini_settings())
    built = fake_chat_vertex[0]
    assert built["model_name"] == "gemini-3.5-flash" and built["project"] == "proj" and built["location"] == "global"
    assert "temperature" not in built and "credentials" not in built  # defaults + Application Default Credentials


def test_the_answer_gets_room_for_gemini_thinking_tokens(fake_chat_vertex):
    make_chat_model(gemini_settings(), max_tokens=2000)
    make_chat_model(gemini_settings())
    assert fake_chat_vertex[0]["max_output_tokens"] == 2000 + GEMINI_THINKING_HEADROOM
    assert fake_chat_vertex[1]["max_output_tokens"] == GEMINI_DEFAULT_MAX_TOKENS + GEMINI_THINKING_HEADROOM


def test_every_gemini_call_has_a_deadline_and_a_retry_limit(fake_chat_vertex):
    make_chat_model(gemini_settings())
    assert fake_chat_vertex[0]["timeout"] == 300 and fake_chat_vertex[0]["max_retries"] == 2


def test_one_agent_can_use_a_stronger_model(fake_chat_vertex):
    make_chat_model(gemini_settings(), model="gemini-3.1-pro-preview")
    assert fake_chat_vertex[0]["model_name"] == "gemini-3.1-pro-preview"


def test_gemini_needs_a_project():
    with pytest.raises(ConfigError, match="GCP_PROJECT"):
        make_chat_model(Settings(_env_file=None, llm_provider="gemini"))


# ------------------------------------------------------------------------------ keyless login
def test_adc_mode_leaves_the_login_to_the_google_libraries():
    assert google_credentials(gemini_settings()) is None


def test_token_mode_uses_the_access_token_you_pass_in():
    creds = google_credentials(gemini_settings(gcp_auth="token", gcp_access_token="ya29.test"))
    assert creds.token == "ya29.test"
    with pytest.raises(ConfigError, match="GCP_ACCESS_TOKEN"):
        google_credentials(gemini_settings(gcp_auth="token"))


def test_gcloud_mode_fetches_a_token_when_needed_and_again_when_it_expires():
    calls = []

    def fake_gcloud():
        calls.append(1)
        return f"ya29.token{len(calls)}"

    creds = google_credentials(gemini_settings(gcp_auth="gcloud"), gcloud_token=fake_gcloud)
    creds.refresh(None)
    assert creds.token == "ya29.token1" and not creds.expired
    creds.refresh(None)
    assert creds.token == "ya29.token2"


def test_an_unknown_auth_mode_is_refused():
    with pytest.raises(ConfigError, match="GCP_AUTH"):
        google_credentials(gemini_settings(gcp_auth="key-file"))


def test_databricks_mode_needs_its_settings():
    with pytest.raises(ConfigError, match="GCP_WIF_AUDIENCE"):
        google_credentials(gemini_settings(gcp_auth="databricks"))


def federated(**overrides):
    base = {"gcp_auth": "databricks", "gcp_wif_audience": AUDIENCE, "gcp_service_account": SERVICE_ACCOUNT,
            "databricks_server_hostname": "dbc-1.cloud.databricks.com", "databricks_client_id": "app-id"}
    return google_credentials(gemini_settings(**{**base, **overrides}), token_supplier=lambda: "databricks-jwt")


def test_databricks_mode_builds_a_federated_login_that_impersonates_the_service_account():
    creds = federated()
    assert creds._audience == AUDIENCE and creds._token_url == STS_URL
    assert creds._service_account_impersonation_url == impersonation_url(SERVICE_ACCOUNT)
    assert creds._subject_token_type == "urn:ietf:params:oauth:token-type:jwt"


def test_the_databricks_token_is_what_gets_exchanged():
    creds = federated()
    assert creds._subject_token_supplier.get_subject_token(None, None) == "databricks-jwt"


def test_the_gemini_model_is_given_the_federated_credentials(fake_chat_vertex):
    make_chat_model(gemini_settings(gcp_auth="databricks", gcp_wif_audience=AUDIENCE, gcp_service_account=SERVICE_ACCOUNT,
                                    databricks_server_hostname="h", databricks_client_id="c",
                                    databricks_client_secret="s"))
    assert fake_chat_vertex[0]["credentials"].__class__.__name__ == "Credentials"


# ---------------------------------------------------------------------------------- pricing
def test_gemini_thinking_tokens_are_billed_as_output():
    usage = {"gemini-3.1-pro-preview": {"input_tokens": 1000, "output_tokens": 500,
                                        "output_token_details": {"reasoning": 1500}}}
    assert usage_cost(usage) == pytest.approx(1000 * 2.00 / 1e6 + 2000 * 12.00 / 1e6)


def test_flash_and_pro_have_different_prices():
    one_million = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert usage_cost({"gemini-3.5-flash": one_million}) == pytest.approx(1.50)
    assert usage_cost({"gemini-3.1-pro-preview": one_million}) == pytest.approx(2.00)
