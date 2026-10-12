"""Faithfulness check: does the evidence actually support each sentence of the narrative?

The Narrative Agent already checks, in plain code, that every dollar amount, date and id it writes
exists in the evidence. That cannot see *meaning*: a sentence can use only correct figures and still
add an invented motive, a made-up relationship or a wrong conclusion. So here a second model call
reads the evidence and the draft side by side and rules on every sentence:

    supported     every claim in it is stated in the evidence
    unsupported   it claims something the evidence does not state
    contradicted  it conflicts with the evidence

A judge can itself be wrong or lazy, so it is not trusted either. Every "supported" verdict must
come with a short quote copied from the evidence, and plain Python checks that the quote really is
there; a verdict whose quote cannot be found is downgraded to "unverified". The result is a report
with pass/fail thresholds. Nothing here edits or deletes the draft - it flags sentences for the
human investigator, who makes the decision.

The judge (`judge_fn`) is injected, so everything is testable without an API key.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from aml_copilot.agents.llm import invoke_structured, make_chat_model
from aml_copilot.agents.models import (
    EvidenceSummary,
    JsonList,
    NarrativeStatement,
    SarNarrative,
    TypologyAssessment,
)
from aml_copilot.config import Settings
from aml_copilot.detection import RiskScore
from aml_copilot.errors import AMLCopilotError
from aml_copilot.guardrails.consistency import check_consistency

JudgedVerdict = Literal["supported", "unsupported", "contradicted"]
FinalVerdict = Literal["supported", "unsupported", "contradicted", "unverified"]


# --------------------------------------------------------------------- what the judge writes
class JudgedStatement(BaseModel):
    """The judge's ruling on one numbered sentence."""

    number: int = Field(description="The sentence's number from the list")
    verdict: JudgedVerdict = Field(description="supported, unsupported or contradicted")
    evidence_quote: str = Field(
        default="",
        description="A short exact quote (under 25 words) copied from the evidence that supports the "
        "sentence or conflicts with it. Empty for an unsupported sentence",
    )
    reason: str = Field(default="", description="One short sentence explaining the verdict")


class FaithfulnessJudgment(BaseModel):
    statements: JsonList[JudgedStatement] = Field(default_factory=list)


# ------------------------------------------------------------------- what the system returns
class StatementCheck(BaseModel):
    number: int
    section: str
    text: str
    verdict: FinalVerdict
    evidence_quote: str = ""
    reason: str = ""


@dataclass(frozen=True)
class FaithfulnessConfig:
    """Pass/fail thresholds: how many flagged sentences a draft may have and still pass.

    Zero everywhere by default: a SAR narrative is a regulatory document, so any sentence the
    evidence does not clearly support goes to the investigator.
    """

    max_unsupported: int = 0
    max_contradicted: int = 0
    max_unverified: int = 0


class FaithfulnessReport(BaseModel):
    account_id: str
    checks: list[StatementCheck] = Field(default_factory=list)
    supported: int = 0
    unsupported: int = 0
    contradicted: int = 0
    unverified: int = 0
    consistency_issues: list[str] = Field(default_factory=list)
    passed: bool = True

    @property
    def flagged(self) -> list[StatementCheck]:
        return [check for check in self.checks if check.verdict != "supported"]


class FaithfulnessState(TypedDict, total=False):
    report: FaithfulnessReport
    error: str


JudgeFn = Callable[[str], FaithfulnessJudgment]  # prompt -> the judge's structured ruling

SYSTEM_PROMPT = """You are a fact-checker for draft anti-money-laundering narratives. You are given \
the evidence for one account and a numbered list of sentences from a draft narrative. For EACH \
sentence, decide whether the evidence supports it.

- supported: every factual claim in the sentence is stated in the evidence or in the typology \
knowledge below. A sentence that only restates the rule engine's output, names a matched typology, \
or says something "could not be assessed" when the evidence lists it as a gap is supported when the \
evidence says so. A sentence that only says the decision or determination rests with the investigator \
is a required disclaimer that makes no claim about the account - it is always supported (for its \
quote, copy any short span of the evidence, such as the account number).
- unsupported: the sentence contains a claim the evidence does not state - even a plausible one. \
Typical examples: an inferred motive or intent, a relationship between people or accounts, a purpose \
or destination of funds, an explanation of why something happened, or any other fact that is not there.
- contradicted: the sentence conflicts with the evidence.

Rules:
- Judge meaning, not wording. Paraphrase and re-ordering are fine; adding a claim is not.
- Check polarity. A sentence that affirms something the evidence denies, or denies something the \
evidence affirms, is contradicted - for example "is a politically exposed person" when the evidence \
says the account is "not flagged as PEP". The same term appearing in the evidence is not support: \
read what the evidence actually says about it.
- Be strict. If you would have to assume something to accept a sentence, it is unsupported.
- evidence_quote: for supported and contradicted sentences, copy a short exact span (under 25 \
words) from the evidence that decides the verdict. Copy it character for character - do not \
paraphrase, summarize or join separate spans. Leave it empty for an unsupported sentence.
- Return one entry for every sentence number, in order. Do not rewrite or comment on the sentences."""


# --------------------------------------------------------------------------- the prompt
def _statements(narrative: SarNarrative) -> list[tuple[str, NarrativeStatement]]:
    """Every sentence of the draft, in reading order, tagged with its section."""
    parts = (("introduction", narrative.introduction), ("body", narrative.facts), ("conclusion", narrative.conclusion))
    return [(section, statement) for section, statements in parts for statement in statements]


def _evidence_block(evidence: EvidenceSummary, risk_score: RiskScore, assessment: TypologyAssessment) -> str:
    """Everything the judge may rely on. Also the yardstick for checking the judge's own quotes, so
    the quotes and the prompt can never disagree about what 'the evidence' is."""
    lines = [
        "=== ACCOUNT EVIDENCE ===",
        f"Account: {evidence.account_id}",
        f"KYC profile: {evidence.profile_summary}",
        f"Transactions: {evidence.transaction_summary}",
        f"Related-account network: {evidence.network_summary}",
        f"Prior cases: {evidence.case_history_summary}",
        f"Detection summary: {evidence.detection_summary}",
        f"Rule engine: score {risk_score.score}/100 ({risk_score.tier})",
        *[f"  - {reason}" for reason in risk_score.reasons],
        "Data completeness notes: " + ("; ".join(evidence.data_completeness) or "none"),
        # the ids matter: they are how the narrative cites a transaction, so the judge must see them
        *[f"Evidence claim: {claim} (ids: {', '.join(ids)})" for claim, ids in evidence.citations.items()],
        "",
        "=== TYPOLOGY KNOWLEDGE (what the documents say, and the matches already validated) ===",
    ]
    if assessment.matches:
        for match in assessment.matches:
            lines.append(f"Matched typology: {match.typology} (confidence {match.confidence})")
            lines.extend(f"  red flag supported by the evidence: {flag}" for flag in match.matched_red_flags)
            lines.extend(f"  red flag not assessable from the data: {flag}" for flag in match.unassessable_red_flags)
            lines.append(f"  analysis: {match.rationale}")
    else:
        lines.append("Matched typology: none")
    matched_sources = {match.source_document for match in assessment.matches}
    excerpts = [p for p in assessment.passages if p.source in matched_sources][:4]
    lines.extend(f"Document excerpt ({p.source}): {p.text[:600]}" for p in excerpts)
    return "\n".join(lines)


def build_faithfulness_prompt(
    evidence: EvidenceSummary, risk_score: RiskScore, assessment: TypologyAssessment, narrative: SarNarrative
) -> str:
    """Everything the judge is told is in this string, so a test can assert against it."""
    numbered = [
        f"{number}. [{section}] {statement.text}"
        for number, (section, statement) in enumerate(_statements(narrative), start=1)
    ]
    return "\n".join(
        [SYSTEM_PROMPT, "", _evidence_block(evidence, risk_score, assessment), "", "=== SENTENCES TO CHECK ===", *numbered]
    )


# ------------------------------------------------- checking the judge's answer (no LLM)
def _normalize(text: str) -> str:
    """Lower-case, quotes removed, whitespace collapsed: the same treatment for both sides."""
    return " ".join(re.sub(r"[\"'‘’“”]", "", text).casefold().split())


def quote_in_evidence(quote: str, evidence_normalized: str) -> bool:
    """True if every fragment of the quote appears in the evidence. A quote may use '...' to skip
    over a stretch of text; each piece on either side of it must still be found."""
    pieces = [_normalize(piece).strip(" .,;:") for piece in re.split(r"\.\.\.|…", quote)]
    pieces = [piece for piece in pieces if len(piece) >= 4]
    return bool(pieces) and all(piece in evidence_normalized for piece in pieces)


def finalize_faithfulness(
    evidence: EvidenceSummary,
    risk_score: RiskScore,
    assessment: TypologyAssessment,
    narrative: SarNarrative,
    judgment: FaithfulnessJudgment,
    config: FaithfulnessConfig | None = None,
) -> FaithfulnessReport:
    """Turn the judge's raw answer into a verified report.

    The judge is allowed to be wrong or lazy; this is where that stops mattering. A sentence it
    skipped, or called supported without a quote that really is in the evidence, becomes
    "unverified" - never a silent pass.
    """
    config = config or FaithfulnessConfig()
    evidence_normalized = _normalize(_evidence_block(evidence, risk_score, assessment))
    ruled: dict[int, JudgedStatement] = {}
    for item in judgment.statements:
        ruled.setdefault(item.number, item)  # if a number appears twice, the first ruling counts

    checks: list[StatementCheck] = []
    for number, (section, statement) in enumerate(_statements(narrative), start=1):
        item = ruled.get(number)
        if item is None:
            verdict: FinalVerdict = "unverified"
            quote, reason = "", "The judge gave no ruling on this sentence"
        elif item.verdict == "supported" and not quote_in_evidence(item.evidence_quote, evidence_normalized):
            verdict = "unverified"
            quote, reason = item.evidence_quote, "The judge called it supported, but its quote is not in the evidence"
        else:
            verdict, quote, reason = item.verdict, item.evidence_quote, item.reason
        checks.append(
            StatementCheck(number=number, section=section, text=statement.text, verdict=verdict,
                           evidence_quote=quote, reason=reason)
        )

    counts = {kind: sum(1 for check in checks if check.verdict == kind)
              for kind in ("supported", "unsupported", "contradicted", "unverified")}
    issues = check_consistency(evidence, risk_score, assessment, narrative)
    passed = (
        counts["unsupported"] <= config.max_unsupported
        and counts["contradicted"] <= config.max_contradicted
        and counts["unverified"] <= config.max_unverified
        and not issues
    )
    return FaithfulnessReport(
        account_id=evidence.account_id, checks=checks, consistency_issues=issues, passed=passed, **counts
    )


def check_faithfulness(
    evidence: EvidenceSummary,
    risk_score: RiskScore,
    assessment: TypologyAssessment,
    narrative: SarNarrative,
    judge_fn: JudgeFn,
    config: FaithfulnessConfig | None = None,
) -> FaithfulnessReport:
    """Ask the judge about every sentence of a drafted narrative and verify what it says."""
    if not _statements(narrative):
        return FaithfulnessReport(account_id=evidence.account_id)  # nothing to check, nothing flagged
    judgment = judge_fn(build_faithfulness_prompt(evidence, risk_score, assessment, narrative))
    return finalize_faithfulness(evidence, risk_score, assessment, narrative, judgment, config)


# ---------------------------------------------------------------- the real wiring
def make_anthropic_faithfulness_judge(settings: Settings) -> JudgeFn:
    """The real judge: Claude via langchain-anthropic, structured to FaithfulnessJudgment."""
    chat = make_chat_model(settings, max_tokens=4096, model=settings.faithfulness_model or None)
    structured = chat.with_structured_output(FaithfulnessJudgment)

    def judge(prompt: str) -> FaithfulnessJudgment:
        return invoke_structured(structured, prompt)

    return judge


def run_faithfulness(
    evidence: EvidenceSummary,
    risk_score: RiskScore,
    assessment: TypologyAssessment,
    narrative: SarNarrative,
    settings: Settings | None = None,
    config: FaithfulnessConfig | None = None,
) -> FaithfulnessState:
    """Run the check against the real Claude. Like the agents, it records an error in its result
    instead of raising for a failure it expects (e.g. the judge's answer not fitting the schema)."""
    settings = settings or Settings()
    judge_fn = make_anthropic_faithfulness_judge(settings)  # fails fast on a missing API key
    try:
        return {"report": check_faithfulness(evidence, risk_score, assessment, narrative, judge_fn, config)}
    except AMLCopilotError as exc:
        return {"error": str(exc)}
