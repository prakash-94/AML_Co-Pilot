"""What an API client sees: a finished investigation, converted to plain JSON-safe data.

The pipeline's own result (`RunState`) holds the raw database rows (`bundle`: every transaction, the
KYC profile, the case records) and Python objects that cannot be turned into JSON as they are. This
module is the single doorway between that and the outside world, and it is built so the raw rows
cannot get through: `InvestigationResult` simply has no field for them. Anything not listed here is
never stored and never returned.
"""

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from aml_copilot.agents.models import EvidenceSummary, SarNarrative, TypologyAssessment
from aml_copilot.guardrails import FaithfulnessReport
from aml_copilot.ids import validate_account_id

RunStatus = Literal["draft_ready", "no_action", "incomplete", "failed"]
Decision = Literal["approve", "reject", "escalate"]


def to_jsonable(value: Any) -> Any:
    """Turn dataclasses, timedeltas, dates and decimals into plain JSON types (recursively)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]
    return value


class Provenance(BaseModel):
    """How this draft was produced - the facts an auditor needs to reproduce or question it."""

    llm_provider: str = ""
    models: dict[str, str] = Field(default_factory=dict)  # role -> model, e.g. {"narrative": "claude-sonnet-5"}
    build: dict[str, str] = Field(default_factory=dict)  # git sha, build time, typology-document hash
    step_seconds: dict[str, float] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class InvestigationResult(BaseModel):
    """One investigation, exactly as stored and returned. Note what is NOT here: `bundle`."""

    account_id: str
    status: RunStatus
    status_detail: str
    steps_completed: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None
    risk_score: dict[str, Any] | None = None
    evidence: EvidenceSummary | None = None
    assessment: TypologyAssessment | None = None
    narrative: SarNarrative | None = None
    narrative_text: str | None = None
    faithfulness: FaithfulnessReport | None = None
    faithfulness_error: str | None = None
    provenance: Provenance = Field(default_factory=Provenance)
    result_hash: str = ""


def compute_result_hash(result: InvestigationResult) -> str:
    """SHA-256 of the result's canonical JSON, excluding the hash field itself.

    A reviewer's decision stores this value, so the audit trail proves exactly which draft was
    approved - a later edit to the stored draft would no longer match.
    """
    canonical = json.dumps(
        result.model_dump(mode="json", exclude={"result_hash"}), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def to_api_result(state: Mapping[str, Any], provenance: Provenance | None = None) -> InvestigationResult:
    """Build the API/storage form of a finished run. Reads the named keys only - never `bundle`."""
    narrative = state.get("narrative")
    result = InvestigationResult(
        account_id=state["account_id"],
        status=state["status"],
        status_detail=state["status_detail"],
        steps_completed=list(state.get("steps_completed", [])),
        missing_sources=list(state.get("missing_sources", [])),
        warnings=list(state.get("warnings", [])),
        failed_step=state.get("failed_step"),
        error=state.get("error"),
        risk_score=to_jsonable(state["risk_score"]) if state.get("risk_score") is not None else None,
        evidence=state.get("evidence"),
        assessment=state.get("assessment"),
        narrative=narrative,
        narrative_text=narrative.render() if narrative is not None else None,
        faithfulness=state.get("faithfulness"),
        faithfulness_error=state.get("faithfulness_error"),
        provenance=provenance or Provenance(),
    )
    return result.model_copy(update={"result_hash": compute_result_hash(result)})


# ------------------------------------------------------------------------- what clients send
class StartRequest(BaseModel):
    account_id: str

    @field_validator("account_id")
    @classmethod
    def must_be_a_valid_account_id(cls, value: str) -> str:
        return validate_account_id(value)  # rejects anything not ACC-[S|L|N|H]-nnnnn, injection included


class DecisionRequest(BaseModel):
    decision: Decision
    comment: str = Field(min_length=1, max_length=2000)
    result_hash: str = Field(min_length=64, max_length=64)  # the draft the reviewer actually read

    @field_validator("comment")
    @classmethod
    def comment_must_say_something(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A decision needs a comment")
        return value.strip()
