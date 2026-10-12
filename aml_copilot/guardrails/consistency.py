"""Deterministic consistency checks: free, instant, and exact - no model involved.

The faithfulness judge (faithfulness.py) reads for *meaning* and costs a model call. These checks
catch the mistakes that have a single right answer in the data, such as a wrong score.
"""

import re

from aml_copilot.agents.models import EvidenceSummary, SarNarrative, TypologyAssessment
from aml_copilot.detection import RiskScore

_SCORE_RE = re.compile(r"\b(\d{1,3})\s*/\s*100\b")
_TIER_AFTER_SCORE_RE = re.compile(r"\b\d{1,3}\s*/\s*100\s*\((low|medium|high)\)", re.IGNORECASE)
_PRIOR_CASES_RE = re.compile(r"\b(\d+)\s+prior\s+cases?\b", re.IGNORECASE)


def _typology_mentioned(typology: str, text: str) -> bool:
    """True if the narrative names the typology: its full name, its name without the bracketed
    part, or the bracketed keyword alone ("Rapid Wire Transfers ... (Layering)" -> "layering")."""
    lowered = text.lower()
    base = typology.split("(")[0].strip().lower()
    keyword = re.findall(r"\(([^)]+)\)", typology)
    candidates = [typology.lower(), base, *[k.strip().lower() for k in keyword]]
    return any(candidate and candidate in lowered for candidate in candidates)


def check_consistency(
    evidence: EvidenceSummary,
    risk_score: RiskScore,
    assessment: TypologyAssessment,
    narrative: SarNarrative,
) -> list[str]:
    """Problems found by comparing the narrative with the facts the code already holds.

    An empty list means nothing inconsistent was found - not that the narrative is correct.
    """
    text = " ".join(s.text for part in (narrative.introduction, narrative.facts, narrative.conclusion) for s in part)
    issues: list[str] = []

    for found in _SCORE_RE.findall(text):
        if int(found) != risk_score.score:
            issues.append(
                f"The narrative gives a score of {found}/100 but the rule engine's score is {risk_score.score}/100"
            )
    for tier in _TIER_AFTER_SCORE_RE.findall(text):
        if tier.lower() != risk_score.tier:
            issues.append(f"The narrative calls the score '{tier.lower()}' but the rule engine's tier is '{risk_score.tier}'")
    for count in _PRIOR_CASES_RE.findall(text):
        if not re.search(rf"\b{count}\b", evidence.case_history_summary):
            issues.append(f"The narrative says {count} prior cases but the case history does not give that number")
    for match in assessment.matches:
        if not _typology_mentioned(match.typology, text):
            issues.append(f"The matched typology '{match.typology}' is not mentioned in the narrative")
    return list(dict.fromkeys(issues))
