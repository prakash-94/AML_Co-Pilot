"""Output models for the Evidence Agent and the Typology/RAG Agent."""

import json
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, Field, ValidationInfo


def _lone_string_to_list(value: object) -> object:
    """Models sometimes answer a list-of-strings field with one bare sentence. Accept that
    (an empty string means an empty list) instead of rejecting the whole answer."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    return value


T = TypeVar("T")


def _json_string_to_list(value: object, info: ValidationInfo) -> object:
    """A list field sometimes arrives as JSON text instead of a list - either the list itself
    ('[{...}, {...}]') or the whole answer wrapped around it ('{"statements": [{...}]}', seen live
    from both models). Parse it back, unwrapping by the field's own name; anything else is left
    for normal validation to judge."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, dict) and isinstance(parsed.get(info.field_name), list):
            return parsed[info.field_name]
        return parsed if isinstance(parsed, list) else value
    return value


# A list that also accepts its own JSON text. The schema the LLM sees is still a plain list.
JsonList = Annotated[list[T], BeforeValidator(_json_string_to_list)]

# Same shape as list[str] to the schema the LLM sees; more forgiving about the one slip
# observed in practice. Every list-of-strings field the LLM fills in uses it.
StringList = Annotated[list[str], BeforeValidator(_lone_string_to_list)]


class EvidenceSummary(BaseModel):
    """A factual, cited account summary.

    Deliberately makes no suspicion judgment - that is Phase 3 (detection) and
    Phase 4's Typology/RAG Agent, downstream of this one. Every sentence here
    should be traceable to `citations`, so the Narrative Agent can later cite
    a specific transaction_id or case_id for every claim in a SAR draft.
    """

    account_id: str
    profile_summary: str = Field(description="KYC facts: occupation, risk rating, PEP status, review date")
    transaction_summary: str = Field(description="The account's own transactions: counts, amounts, types, dates")
    network_summary: str = Field(
        description="Related accounts and transactions found by network expansion, with hop distance"
    )
    case_history_summary: str = Field(description="Prior alerts and their dispositions")
    detection_summary: str = Field(
        description="What the automated rule-based detectors (structuring, layering, risk score) "
        "found, reported as their output - not your own suspicion assessment"
    )
    data_completeness: StringList = Field(
        default_factory=list, description="Gaps in the evidence: missing sources, truncated results, no profile"
    )
    citations: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Claim key -> supporting transaction_id / case_id values, e.g. "
        '{"three cash deposits near $8,000": ["TXN-001", "TXN-002", "TXN-003"]}',
    )


# ----------------------------------------------------------------------------------------
# Typology/RAG Agent
#
# Two layers on purpose. `TypologyMatch` / `TypologyJudgment` are what the LLM is allowed to
# fill in. `RankedTypologyMatch` / `TypologyAssessment` are what the system returns *after*
# validating the model's answer in code - they carry fields only code is allowed to set, so
# the model can never claim, for example, that a rule-based detector agreed with it.
# ----------------------------------------------------------------------------------------

Confidence = Literal["high", "medium", "low"]


class TypologyMatch(BaseModel):
    """One documented typology the evidence resembles, as judged by the LLM."""

    typology: str = Field(description="Typology name, e.g. 'Structuring (Smurfing)'")
    source_document: str = Field(
        description="Exact filename of the retrieved typology document this match is based on"
    )
    confidence: Confidence = Field(description="high, medium or low - see the instructions")
    matched_red_flags: StringList = Field(
        default_factory=list, description="Red flags from the passages that the evidence supports"
    )
    unassessable_red_flags: StringList = Field(
        default_factory=list, description="Red flags the available evidence has no data to assess"
    )
    rationale: str = Field(description="Why the evidence resembles this typology, and where it does not")
    evidence_ids: StringList = Field(
        default_factory=list,
        description="Ids only, taken from the list provided - no descriptions - that support this match",
    )


class TypologyJudgment(BaseModel):
    """The LLM's whole answer: ranked-by-the-model matches, or none."""

    matches: JsonList[TypologyMatch] = Field(default_factory=list)
    no_match_reason: str = Field(default="", description="Why nothing matched, if matches is empty")


class RankedTypologyMatch(TypologyMatch):
    """A validated match. Adds a field the LLM never sees and cannot set."""

    # True/False when a rule-based detector exists for this typology and did/did not flag it;
    # None when no detector exists for it (most typologies - see typology_docs/README.md).
    detector_corroborated: bool | None = None


class PassageRef(BaseModel):
    """A retrieved passage the agent was shown - kept on the output so it is auditable."""

    source: str
    heading: str | None = None
    page: int | None = None
    distance: float
    text: str


class TypologyAssessment(BaseModel):
    account_id: str
    matches: list[RankedTypologyMatch] = Field(default_factory=list)
    no_match_reason: str = ""
    passages: list[PassageRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------------------
# Narrative Agent
#
# Same two-layer idea as above. `NarrativeText` is all the LLM may write: three lists of cited
# sentences. `SarNarrative` is what the system returns after checking them in code, and it adds
# what only code may set - the status, and the investigator questions (copied word for word from
# the typology documents, never written by the model).
# ----------------------------------------------------------------------------------------


class NarrativeStatement(BaseModel):
    """One sentence of the narrative and the evidence it rests on."""

    text: str = Field(
        description="One factual sentence. Use only dates, amounts and ids that appear in the evidence"
    )
    evidence_ids: StringList = Field(
        default_factory=list,
        description="Transaction or case ids from the list provided that support this sentence. "
        "Leave empty for sentences about the KYC profile or the rule engine's findings",
    )


class NarrativeText(BaseModel):
    """The LLM's whole answer: the three parts FinCEN's narrative guidance asks for."""

    introduction: JsonList[NarrativeStatement] = Field(
        min_length=1, description="One or two sentences: why this account is referred for review"
    )
    # Named `facts`, not `body`: in testing Claude Sonnet 5 repeatedly mis-filled a tool field
    # called `body` (it packed the whole answer into it as a JSON string); `facts` filled correctly.
    facts: JsonList[NarrativeStatement] = Field(
        min_length=1,
        description="The body of the narrative - who, what, when, where, why and how - one fact per "
        "sentence, in chronological order",
    )
    conclusion: JsonList[NarrativeStatement] = Field(
        min_length=1,
        description="What the evidence shows, what could not be assessed, what is still to be verified",
    )


NarrativeStatus = Literal["draft_for_human_review", "not_drafted"]


class SarNarrative(BaseModel):
    """A checked SAR narrative draft - or the reason none was drafted."""

    account_id: str
    status: NarrativeStatus
    skip_reason: str = ""
    introduction: list[NarrativeStatement] = Field(default_factory=list)
    facts: list[NarrativeStatement] = Field(default_factory=list)
    conclusion: list[NarrativeStatement] = Field(default_factory=list)
    typologies: list[str] = Field(default_factory=list, description="Matched typology names (set by code)")
    open_questions: list[str] = Field(
        default_factory=list, description="Questions for the investigator, from the typology documents"
    )
    warnings: list[str] = Field(default_factory=list)

    def render(self) -> str:
        """Plain-text version for a person to read; every sentence is followed by its ids."""
        if self.status == "not_drafted":
            return f"No narrative drafted for {self.account_id}: {self.skip_reason}"

        def section(title: str, statements: list[NarrativeStatement]) -> list[str]:
            lines = [title]
            if not statements:
                lines.append("  (nothing - every statement failed validation, see warnings)")
            for statement in statements:
                ids = f" [{', '.join(statement.evidence_ids)}]" if statement.evidence_ids else ""
                lines.append(f"  {statement.text}{ids}")
            return [*lines, ""]

        out = [f"SAR NARRATIVE DRAFT - {self.account_id} (pending human review)", ""]
        out += section("Introduction", self.introduction)
        out += section("Body", self.facts)
        out += section("Conclusion", self.conclusion)
        if self.open_questions:
            out.append("Questions for the investigator to verify")
            out += [f"  - {question}" for question in self.open_questions]
            out.append("")
        if self.warnings:
            out.append("Warnings")
            out += [f"  - {warning}" for warning in self.warnings]
        return "\n".join(out).rstrip()
