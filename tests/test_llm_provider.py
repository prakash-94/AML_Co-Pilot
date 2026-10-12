"""Choosing where Claude is called from: the Anthropic API, or Claude on Vertex AI."""

import sys
import types

import pytest
from pydantic import SecretStr

from aml_copilot.agents.llm import PROVIDERS, make_chat_model, vertex_model_id
from aml_copilot.config import Settings
from aml_copilot.errors import ConfigError
from aml_copilot.evaluation.cost import usage_cost


def vertex_settings(**overrides):
    base = {"_env_file": None, "llm_provider": "vertex", "gcp_project": "my-project", "anthropic_model": "claude-sonnet-5"}
    return Settings(**{**base, **overrides})


@pytest.fixture
def fake_vertex(monkeypatch):
    """Stands in for langchain_google_vertexai (not installed in the test environment): records what it was built with."""
    built = []

    class FakeChatAnthropicVertex:
        def __init__(self, **kwargs):
            built.append(kwargs)

    module = types.ModuleType("langchain_google_vertexai.model_garden")
    module.ChatAnthropicVertex = FakeChatAnthropicVertex
    monkeypatch.setitem(sys.modules, "langchain_google_vertexai", types.ModuleType("langchain_google_vertexai"))
    monkeypatch.setitem(sys.modules, "langchain_google_vertexai.model_garden", module)
    return built


# ----------------------------------------------------------------------------- the id map
@pytest.mark.parametrize(("name", "expected"), [
    ("claude-sonnet-5", "claude-sonnet-5"),
    ("claude-haiku-4-5", "claude-haiku-4-5@20251001"),
    ("claude-haiku-4-5@20251001", "claude-haiku-4-5@20251001"),  # already a Vertex id: untouched
    ("some-future-model", "some-future-model"),  # unknown names pass through rather than failing here
])
def test_logical_model_names_become_vertex_ids(name, expected):
    assert vertex_model_id(name) == expected


def test_an_environment_override_beats_the_built_in_map():
    assert vertex_model_id("claude-haiku-4-5", {"claude-haiku-4-5": "claude-haiku-4-5@99999999"}) == "claude-haiku-4-5@99999999"


# ------------------------------------------------------------------------ the vertex branch
def test_vertex_builds_the_model_for_the_project_on_the_global_endpoint(fake_vertex):
    make_chat_model(vertex_settings(), max_tokens=4096)
    assert fake_vertex == [{"model": "claude-sonnet-5", "project": "my-project", "location": "global", "max_tokens": 4096}]


def test_sonnet_5_is_never_sent_a_temperature_because_it_rejects_one(fake_vertex):
    make_chat_model(vertex_settings())
    assert "temperature" not in fake_vertex[0]


def test_haiku_gets_temperature_zero_and_its_dated_vertex_id(fake_vertex):
    make_chat_model(vertex_settings(), model="claude-haiku-4-5")
    assert fake_vertex[0]["temperature"] == 0 and fake_vertex[0]["model"] == "claude-haiku-4-5@20251001"


def test_a_per_agent_model_override_works_on_vertex_too(fake_vertex):
    make_chat_model(vertex_settings(anthropic_model="claude-haiku-4-5"), model="claude-sonnet-5")
    assert fake_vertex[0]["model"] == "claude-sonnet-5" and "temperature" not in fake_vertex[0]


def test_max_tokens_is_passed_only_when_asked_for(fake_vertex):
    make_chat_model(vertex_settings())
    assert "max_tokens" not in fake_vertex[0]


def test_vertex_uses_the_configured_location_and_id_overrides(fake_vertex):
    settings = vertex_settings(vertex_location="us", vertex_model_ids={"claude-sonnet-5": "claude-sonnet-5@v2"})
    make_chat_model(settings)
    assert fake_vertex[0]["location"] == "us" and fake_vertex[0]["model"] == "claude-sonnet-5@v2"


def test_the_id_map_can_be_set_from_an_environment_variable(monkeypatch):
    monkeypatch.setenv("VERTEX_MODEL_IDS", '{"claude-haiku-4-5": "claude-haiku-4-5@20260101"}')
    assert Settings(_env_file=None).vertex_model_ids == {"claude-haiku-4-5": "claude-haiku-4-5@20260101"}


def test_vertex_needs_a_project_but_no_anthropic_key(fake_vertex):
    make_chat_model(vertex_settings())  # no ANTHROPIC_API_KEY anywhere: fine
    with pytest.raises(ConfigError, match="GCP_PROJECT"):
        make_chat_model(vertex_settings(gcp_project=""))


# ---------------------------------------------------------------- choosing / defaults
def test_the_default_provider_is_gemini_on_vertex():
    assert Settings(_env_file=None).llm_provider == "gemini" and PROVIDERS == ("gemini", "anthropic", "vertex")


def test_the_anthropic_provider_still_demands_its_key(monkeypatch):
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_chat_model(Settings(_env_file=None, llm_provider="anthropic"))


def test_the_anthropic_provider_does_not_need_google_libraries(monkeypatch):
    built = []

    class FakeChatAnthropic:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", FakeChatAnthropic)
    make_chat_model(Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key=SecretStr("sk-test"),
                             anthropic_model="claude-sonnet-5"))
    assert built[0]["model"] == "claude-sonnet-5"  # untouched by the new provider code


def test_an_unknown_provider_is_a_clear_error():
    with pytest.raises(ConfigError, match="LLM_PROVIDER must be one of"):
        make_chat_model(Settings(_env_file=None, llm_provider="bedrock"))


# --------------------------------------------------------- costs are still measured on vertex
def test_vertex_model_names_are_priced_by_the_same_substring_rule():
    usage = {
        "claude-haiku-4-5@20251001": {"input_tokens": 1_000_000, "output_tokens": 0},  # $1
        "claude-sonnet-5": {"input_tokens": 0, "output_tokens": 1_000_000},  # $10
    }
    assert usage_cost(usage) == pytest.approx(11.0)
