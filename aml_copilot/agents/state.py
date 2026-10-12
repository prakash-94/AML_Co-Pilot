"""LangGraph state for the Evidence Agent."""

from typing import TypedDict

from aml_copilot.agents.models import EvidenceSummary
from aml_copilot.detection import RiskScore
from aml_copilot.ingestion import EvidenceBundle


class InvestigationState(TypedDict, total=False):
    account_id: str
    bundle: EvidenceBundle
    risk_score: RiskScore
    summary: EvidenceSummary
    error: str
