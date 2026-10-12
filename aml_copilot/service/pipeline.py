"""The real pipeline, wired for the service: how a job runs one investigation and what it records about it."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from aml_copilot.config import Settings
from aml_copilot.service.models import Provenance


def utc_now() -> datetime:
    return datetime.now(UTC)


def make_run_fn(settings: Settings) -> Callable[[str], Mapping[str, Any]]:
    """The real pipeline. Imported only here, so everything else stays free of torch and the embedder."""

    def run(account_id: str) -> Mapping[str, Any]:
        from aml_copilot.agents.investigation import run_investigation

        timings: dict[str, float] = {}
        state = run_investigation(account_id, settings, observer=timings.__setitem__)
        return {**state, "step_seconds": timings}

    return run


def make_provenance(settings: Settings) -> Provenance:
    models = {
        "evidence": settings.anthropic_model,
        "typology": settings.typology_model or settings.anthropic_model,
        "narrative": settings.narrative_model or settings.anthropic_model,
        "faithfulness": settings.faithfulness_model or settings.anthropic_model,
    }
    return Provenance(llm_provider=settings.llm_provider, models=models, build={"sha": settings.build_sha})
