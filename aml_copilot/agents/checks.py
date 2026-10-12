"""Plain-Python checks shared by the agents that ask Claude for structured output.

Each agent sandwiches the LLM between deterministic code. These are the pieces of that code
that more than one agent needs, so they live in one place instead of being copied.
"""

from aml_copilot.detection import RiskScore

# Wording that suggests the model slid from "resembles a documented pattern" into a verdict.
# A crude tripwire, not a guarantee: callers never edit or drop anything because of it, they only
# raise a warning so the human reviewer looks at that wording. (A proper faithfulness check
# belongs to Phase 5.)
VERDICT_PHRASES = (
    "suspicious", "illegitimate", "criminal", "guilty", "should file", "file a sar", "recommend filing",
)


def verdict_words(text: str) -> list[str]:
    """The verdict-like phrases found in `text` (case-insensitive)."""
    lowered = text.lower()
    return [phrase for phrase in VERDICT_PHRASES if phrase in lowered]


def rule_engine_ids(risk_score: RiskScore) -> list[str]:
    """Transaction ids the rule engine itself flagged. These come from deterministic code, not
    from an LLM, so they are always safe to cite - even when the Evidence Agent's own LLM-written
    citations happened to leave them out."""
    ids: list[str] = []
    if risk_score.structuring and risk_score.structuring.flagged:
        ids.extend(risk_score.structuring.matched_transaction_ids)
    if risk_score.layering and risk_score.layering.flagged:
        ids.extend(risk_score.layering.chain_transaction_ids)
    return list(dict.fromkeys(ids))


def clean_ids(raw_ids: list[str], valid_ids: set[str]) -> list[str]:
    """Tidy the ids a model returned: trim stray spaces, de-duplicate (keeping order), and split
    a comma-joined string - but ONLY when every piece is a real id. Anything else (a sentence
    that happens to contain commas) is left whole, so a warning can show it intact."""
    cleaned: list[str] = []
    for raw in raw_ids:
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        if len(parts) > 1 and all(part in valid_ids for part in parts):
            cleaned.extend(parts)
        else:
            cleaned.append(raw.strip())
    return list(dict.fromkeys(item for item in cleaned if item))
