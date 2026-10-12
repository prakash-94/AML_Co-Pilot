"""Combines the Structuring and Layering signals into one risk score per account.

Purely mechanical - no LLM here. The Evidence Agent later reports this score as a fact
("the rule engine found X"); it does not render its own suspicion judgment on top of it.
"""

from dataclasses import dataclass, field

from aml_copilot.detection.layering import LayeringConfig, LayeringSignal, detect_layering
from aml_copilot.detection.structuring import (
    StructuringConfig,
    StructuringSignal,
    detect_structuring,
)
from aml_copilot.ingestion import EvidenceBundle


@dataclass(frozen=True)
class RiskScoreConfig:
    structuring_weight: int = 50
    layering_weight: int = 50
    high_threshold: int = 70
    medium_threshold: int = 30


@dataclass
class RiskScore:
    score: int
    tier: str  # "low" | "medium" | "high"
    reasons: list[str] = field(default_factory=list)
    structuring: StructuringSignal | None = None
    layering: LayeringSignal | None = None


def score_account(
    structuring: StructuringSignal,
    layering: LayeringSignal,
    config: RiskScoreConfig | None = None,
) -> RiskScore:
    cfg = config or RiskScoreConfig()

    score = 0
    reasons: list[str] = []
    if structuring.flagged:
        score += cfg.structuring_weight
        reasons.append(f"Structuring signal: {structuring.reason}")
    if layering.flagged:
        score += cfg.layering_weight
        reasons.append(f"Layering signal: {layering.reason}")
    score = min(score, 100)

    if score >= cfg.high_threshold:
        tier = "high"
    elif score >= cfg.medium_threshold:
        tier = "medium"
    else:
        tier = "low"

    if not reasons:
        reasons.append("No structuring or layering signals detected")

    return RiskScore(score=score, tier=tier, reasons=reasons, structuring=structuring, layering=layering)


def run_detection(
    bundle: EvidenceBundle,
    structuring_config: StructuringConfig | None = None,
    layering_config: LayeringConfig | None = None,
    score_config: RiskScoreConfig | None = None,
) -> RiskScore:
    """Convenience: run both detectors on a bundle and combine into one score."""
    structuring = detect_structuring(bundle.transactions, structuring_config)
    layering = detect_layering(bundle, layering_config)
    return score_account(structuring, layering, score_config)
