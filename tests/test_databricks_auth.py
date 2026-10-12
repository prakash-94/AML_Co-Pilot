"""OAuth for Databricks: tokens are fetched, cached, renewed before expiry, and failures never leak the secret."""

import pytest

from aml_copilot.config import Settings
from aml_copilot.errors import SourceUnavailableError
from aml_copilot.ingestion.databricks_auth import DatabricksOAuthTokenProvider
from aml_copilot.ingestion.sources import DatabricksSource


class FakeResponse:
    def __init__(self, body, fail=False):
        self._body, self._fail = body, fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("401 Unauthorized for client secret-value-123")

    def json(self):
        return self._body


class FakePost:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def provider(post, clock=None):
    return DatabricksOAuthTokenProvider("dbc-1.cloud.databricks.com", "cid", "secret-value-123",
                                        post=post, clock=clock or Clock())


def test_the_token_is_requested_with_the_client_credentials_grant():
    post = FakePost(FakeResponse({"access_token": "t1", "expires_in": 3600}))
    assert provider(post).token() == "t1"
    url, kwargs = post.calls[0]
    assert url == "https://dbc-1.cloud.databricks.com/oidc/v1/token"
    assert kwargs["auth"] == ("cid", "secret-value-123")
    assert kwargs["data"] == {"grant_type": "client_credentials", "scope": "all-apis"}


def test_the_token_is_reused_until_shortly_before_it_expires():
    clock = Clock()
    post = FakePost(FakeResponse({"access_token": "t1", "expires_in": 3600}),
                    FakeResponse({"access_token": "t2", "expires_in": 3600}))
    p = provider(post, clock)
    assert p.token() == "t1"
    clock.now += 3000  # 10 minutes left: still fine
    assert p.token() == "t1" and len(post.calls) == 1
    clock.now += 400  # under 5 minutes left: renew
    assert p.token() == "t2" and len(post.calls) == 2


def test_a_failed_sign_in_is_reported_without_the_secret():
    p = provider(FakePost(FakeResponse({}, fail=True)))
    with pytest.raises(SourceUnavailableError) as caught:
        p.token()
    assert "secret-value-123" not in str(caught.value) and "RuntimeError" in str(caught.value)


def test_the_credentials_provider_gives_a_bearer_header():
    p = provider(FakePost(FakeResponse({"access_token": "t1", "expires_in": 3600})))
    assert p.credentials_provider()()() == {"Authorization": "Bearer t1"}


def settings(**overrides):
    base = {"_env_file": None, "databricks_server_hostname": "h", "databricks_http_path": "/p"}
    return Settings(**{**base, **overrides})


def test_the_source_uses_oauth_when_a_service_principal_is_configured():
    seen = {}
    source = DatabricksSource(settings(databricks_client_id="cid", databricks_client_secret="s"),
                              connect=lambda **kw: seen.update(kw))
    source._open()
    assert "credentials_provider" in seen and "access_token" not in seen


def test_the_source_falls_back_to_the_token_when_there_is_no_service_principal():
    seen = {}
    source = DatabricksSource(settings(databricks_token="pat"), connect=lambda **kw: seen.update(kw))
    source._open()
    assert seen["access_token"] == "pat" and "credentials_provider" not in seen
