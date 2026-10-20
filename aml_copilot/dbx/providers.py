"""The two model families an investigation can use, chosen per run (from the web page's drop-down or the job parameter).

* `gemini`  Google Gemini on Vertex AI. The model names come from the bundle (`evidence_model`, `judge_model`).
* `claude`  Anthropic's Claude through the Anthropic API (needs ANTHROPIC_API_KEY in the secret scope). The same split as
            the Phase 5 measurements: the cheaper Haiku summarizes, Sonnet judges, writes and fact-checks.
"""

from aml_copilot.errors import ConfigError

PROVIDERS = ("gemini", "claude")
DEFAULT_PROVIDER = "gemini"

CLAUDE_SETTINGS = {
    "LLM_PROVIDER": "anthropic",
    "LLM_MODEL": "claude-haiku-4-5",
    "TYPOLOGY_MODEL": "claude-sonnet-5",
    "NARRATIVE_MODEL": "claude-sonnet-5",
    "FAITHFULNESS_MODEL": "claude-sonnet-5",
}


def provider_settings(provider: str) -> list[str]:
    """`KEY=VALUE` settings that switch a run to `provider` (none for Gemini, whose settings come from the bundle)."""
    if provider == "gemini":
        return []
    if provider == "claude":
        return [f"{key}={value}" for key, value in CLAUDE_SETTINGS.items()]
    raise ConfigError(f"provider must be one of {PROVIDERS}, not {provider!r}")
