"""Helpers shared by every agent that asks Claude for structured output."""

from typing import Any

from pydantic import ValidationError

from aml_copilot.config import Settings, require
from aml_copilot.errors import ConfigError, ModelOutputError

# Models verified (or documented) to accept an explicit temperature. Newer models - including
# claude-sonnet-5 - reject it with a 400, so any model not listed here is called without one:
# slightly more variable, but never broken.
_TEMPERATURE_OK_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6")

PROVIDERS = ("anthropic", "vertex")

# The names used in settings (ANTHROPIC_MODEL, TYPOLOGY_MODEL, ...) -> the id Vertex AI uses.
# Current-generation models use the plain name; models with a dated snapshot use "name@date".
# Haiku 4.5's id is the expected one and is confirmed in Phase 0 against the Model Garden card;
# if it differs, set VERTEX_MODEL_IDS in the environment instead of editing this file.
_VERTEX_MODEL_IDS = {
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-haiku-4-5": "claude-haiku-4-5@20251001",
}


def vertex_model_id(name: str, overrides: dict[str, str] | None = None) -> str:
    """Vertex AI's id for a model name from settings. A name that is already a Vertex id (it has
    an "@" snapshot, or is simply not in the map) passes through unchanged."""
    return {**_VERTEX_MODEL_IDS, **(overrides or {})}.get(name, name)


def make_chat_model(
    settings: Settings, max_tokens: int | None = None, model: str | None = None
) -> Any:
    """Build the Claude chat model, sending temperature=0 only to models that accept it.

    `model` overrides `settings.anthropic_model` for one agent (e.g. a stronger judge).
    `settings.llm_provider` picks where Claude is called from; every agent comes through here, so
    this is the only place that knows the difference.
    """
    model_name = model or settings.anthropic_model
    if settings.llm_provider == "vertex":
        return _make_vertex_model(settings, model_name, max_tokens)
    if settings.llm_provider == "anthropic":
        return _make_anthropic_model(settings, model_name, max_tokens)
    raise ConfigError(f"LLM_PROVIDER must be one of {PROVIDERS}, not {settings.llm_provider!r}")


def _make_anthropic_model(settings: Settings, model_name: str, max_tokens: int | None) -> Any:
    require(settings, "anthropic_api_key")
    from langchain_anthropic import ChatAnthropic

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": settings.anthropic_api_key.get_secret_value(),
    }
    if model_name.startswith(_TEMPERATURE_OK_PREFIXES):
        kwargs["temperature"] = 0
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return ChatAnthropic(**kwargs)


def _make_vertex_model(settings: Settings, model_name: str, max_tokens: int | None) -> Any:
    """Claude on Google Cloud. No API key: it authenticates as the service's own Google identity
    (Application Default Credentials). `location` must be "global" (or a US/EU multi-region) for
    Sonnet 5 - single regions only serve older models."""
    require(settings, "gcp_project")
    from langchain_google_vertexai.model_garden import ChatAnthropicVertex

    kwargs: dict[str, Any] = {
        "model": vertex_model_id(model_name, settings.vertex_model_ids),
        "project": settings.gcp_project,
        "location": settings.vertex_location,
    }
    if model_name.startswith(_TEMPERATURE_OK_PREFIXES):  # same rule: keyed on the logical name
        kwargs["temperature"] = 0
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return ChatAnthropicVertex(**kwargs)


def invoke_structured(structured: Any, prompt: str, attempts: int = 2) -> Any:
    """Call a `with_structured_output(...)` chain, retrying a schema mismatch once.

    An LLM will occasionally return something that does not fit the schema it was given - seen
    live as an intermittent slip, the same prompt failing twice and then succeeding six times
    running. So the call is tried again (`attempts` tries in all). If every try fails, that is an
    expected failure, not a bug in the calling code, so it surfaces as `ModelOutputError` (an
    `AMLCopilotError`, which the agents record in their state) instead of a stack trace.
    """
    last_error: ValidationError | None = None
    for _ in range(max(1, attempts)):
        try:
            return structured.invoke(prompt)
        except ValidationError as exc:
            last_error = exc  # only a schema mismatch is retried; any other error propagates at once

    first = last_error.errors()[0]
    field = ".".join(str(part) for part in first["loc"])
    # Say what arrived, so the next failure explains itself (e.g. "it sent a str: '[{...'").
    received = f" (it sent a {type(first.get('input')).__name__}: {str(first.get('input'))[:60]!r})"
    raise ModelOutputError(
        f"The model's answer did not match the required schema at '{field}': {first['msg']}{received}"
    ) from last_error
