"""Narrative Agent: drafts a SAR narrative from the evidence, the risk score and the typology match.

    prepare --(worth drafting)--> write --> END
        |
        `--(nothing to report / error)---> END

`prepare` decides, in plain code, whether a narrative is warranted at all (a clean account with no
typology match gets none) and loads the investigator questions from the matched typology
documents. `write` shows Claude only the facts already gathered and asks for the three parts
FinCEN's guidance recommends - introduction, body, conclusion - as a list of cited sentences.
Everything Claude writes is then checked in plain Python (`finalize_narrative`): a sentence that
contains a date, amount or id found nowhere in the evidence is dropped, ids that do not exist are
stripped, and wording that reads like a verdict raises a warning. The agent drafts an
*explanation of what the evidence shows*; it never decides that a report should be filed - that
stays with the human investigator at the approval gate.

The writing rules mirror `typology_docs/sar_narrative_standards.md`. Dependencies
(`questions_fn`, `write_fn`) are injected, so the graph is testable without an Anthropic API key.
"""

import re
import sys
from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, StateGraph

from aml_copilot.agents.checks import clean_ids, rule_engine_ids, verdict_words
from aml_copilot.agents.evidence import investigate
from aml_copilot.agents.llm import invoke_structured, make_chat_model
from aml_copilot.agents.models import (
    EvidenceSummary,
    NarrativeStatement,
    NarrativeText,
    SarNarrative,
    TypologyAssessment,
)
from aml_copilot.agents.typology import match_typology
from aml_copilot.config import Settings
from aml_copilot.detection import RiskScore
from aml_copilot.errors import AMLCopilotError
from aml_copilot.logging_config import configure_logging_from_settings
from aml_copilot.retrieval.questions import questions_for_sources


class NarrativeState(TypedDict, total=False):
    evidence: EvidenceSummary
    risk_score: RiskScore
    assessment: TypologyAssessment
    questions: list[str]
    narrative: SarNarrative
    error: str


QuestionsFn = Callable[[list[str]], list[str]]  # typology document names -> investigator questions
WriteFn = Callable[[str], NarrativeText]  # prompt -> the model's structured answer

SYSTEM_PROMPT = """You are drafting a Suspicious Activity Report (SAR) narrative for a human \
investigator to review, edit and decide on. You are given a factual evidence summary, the output \
of a separate deterministic rule engine, and the documented typologies the evidence was matched \
to. Write the narrative in the three parts FinCEN's guidance recommends.

Structure:
- introduction: one or two sentences saying why this account is being referred for review.
- facts: the body of the narrative - who, what, when, where, why and how. Begin with who the \
account holder is, from the KYC profile (including PEP status if flagged), then give the \
transactions in date order. One fact per sentence, with the specific date and each individual \
amount - never a rounded or combined figure. "Why" is what the rule engine and the matched \
typologies found; "how" is the pattern of the activity (for example the chain of transfers).
- conclusion: one to three sentences - what the evidence shows, what could not be assessed \
because the data is missing, and that the decision rests with the investigator.

Rules:
- Use only facts stated below. Never write a date, amount, account id or transaction id that does \
not appear below. Copy figures and dates exactly as written. Do not calculate anything: no totals, \
differences, percentages or durations of your own.
- Describe what the evidence shows and which documented pattern it resembles ("the activity is \
consistent with the pattern described for ..."). Never state or imply that the account holder \
committed a crime or that activity was suspicious, illegitimate or criminal, and never recommend \
filing or not filing a report - that decision belongs to the human investigator.
- evidence_ids: for every sentence about specific transactions or cases, list the ids that support \
it, taken from the list provided. Sentences about the KYC profile or the rule engine's score carry \
no ids. Never invent an id.
- Red flags listed as not assessable, and gaps listed under data completeness, are unknown - say \
they could not be assessed. Never describe them as absent, unexplained or undocumented.
- The account holder's occupation, risk rating and PEP status are context only. State them \
factually under "who"; never say or imply that they make the activity more or less plausible.
- Mention prior cases briefly, in one or two sentences: how many there are and any prior SAR \
filings. Do not list them one by one, and never treat a prior case as proof of anything.
- If no typology was matched, say that the narrative rests on the rule engine's findings alone.
- Do not answer the investigator's follow-up questions; they are added separately."""

_MEDIUM_OR_HIGHER = ("medium", "high")


# --------------------------------------------------------------- the gate (no LLM)
def should_draft(risk_score: RiskScore, assessment: TypologyAssessment) -> bool:
    """A narrative is only worth drafting when something was found: a validated typology match,
    or a rule-engine score at the medium tier or above."""
    return bool(assessment.matches) or risk_score.tier in _MEDIUM_OR_HIGHER


# ---------------------------------------------------------------------------- prompt
def _corroboration(corroborated: bool | None) -> str:
    if corroborated is None:
        return "no rule-based detector exists for this typology"
    return "the rule engine independently flagged this pattern" if corroborated else (
        "the rule engine did NOT flag this pattern"
    )


def build_narrative_prompt(
    evidence: EvidenceSummary, risk_score: RiskScore, assessment: TypologyAssessment
) -> str:
    """Everything the model is told is in this string, so a test can assert against it."""
    parts = [SYSTEM_PROMPT, "", f"Account: {evidence.account_id}", ""]
    parts.append(f"KYC profile: {evidence.profile_summary}")
    parts.append(f"Transactions: {evidence.transaction_summary}")
    parts.append(f"Related-account network: {evidence.network_summary}")
    parts.append(f"Prior cases: {evidence.case_history_summary}")
    parts.append("")

    parts.append(f"Rule engine (separate, deterministic): score {risk_score.score}/100 ({risk_score.tier})")
    parts.extend(f"  - {reason}" for reason in risk_score.reasons)
    parts.append("")

    if assessment.matches:
        parts.append("Matched typologies (already validated):")
        for match in assessment.matches:
            parts.append(
                f"- {match.typology} (document {match.source_document}), confidence {match.confidence}; "
                f"{_corroboration(match.detector_corroborated)}"
            )
            parts.extend(f"    red flag supported: {flag}" for flag in match.matched_red_flags)
            parts.extend(f"    red flag NOT assessable from the data: {flag}" for flag in match.unassessable_red_flags)
            parts.append(f"    analysis: {match.rationale}")
    else:
        parts.append("Matched typologies: none")
    parts.append("")

    gaps = evidence.data_completeness
    parts.append("Data completeness notes: " + ("; ".join(gaps) if gaps else "none"))
    parts.append("")

    parts.append("Evidence ids available for citation (claim -> ids):")
    if evidence.citations:
        parts.extend(f"  - {claim}: {', '.join(ids)}" for claim, ids in evidence.citations.items())
    else:
        parts.append("  (none)")
    extra = rule_engine_ids(risk_score)
    if extra:
        parts.append(f"Transaction ids flagged by the rule engine (also citable): {', '.join(extra)}")
    return "\n".join(parts)


# ------------------------------------------------- checking the model's answer (no LLM)
_MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ID_RE = re.compile(r"\b(?:TXN\d+|(?:CASE|ACC)(?:-[A-Z0-9]+)+)\b")
# Transaction and case ids only: a sentence that names one has cited it, even if the model left it
# out of the `evidence_ids` field. Account ids are left out - they appear in almost every sentence.
_RECORD_ID_RE = re.compile(r"\b(?:TXN\d+|CASE(?:-[A-Z0-9]+)+)\b")


def _source_text(evidence: EvidenceSummary, risk_score: RiskScore, assessment: TypologyAssessment) -> str:
    """Every piece of text the model was entitled to draw on, joined - the yardstick for 'does this
    date, amount or id appear in the evidence?'"""
    parts = [
        evidence.account_id, evidence.profile_summary, evidence.transaction_summary,
        evidence.network_summary, evidence.case_history_summary, evidence.detection_summary,
        *evidence.data_completeness, *risk_score.reasons, *rule_engine_ids(risk_score),
    ]
    for claim, ids in evidence.citations.items():
        parts.extend([claim, *ids])
    for match in assessment.matches:
        parts.extend([match.typology, match.rationale, *match.matched_red_flags, *match.unassessable_red_flags])
    parts.extend(passage.text for passage in assessment.passages)
    return "\n".join(parts)


def _amounts(text: str) -> set[float]:
    return {round(float(raw.replace(",", "")), 2) for raw in _MONEY_RE.findall(text)}


def _unsupported_details(statement: str, source_text: str, source_amounts: set[float]) -> list[str]:
    """Amounts, dates and ids in `statement` that appear nowhere in the evidence."""
    problems = [
        f"amount ${raw}"
        for raw in _MONEY_RE.findall(statement)
        if round(float(raw.replace(",", "")), 2) not in source_amounts
    ]
    problems += [f"date {date}" for date in _DATE_RE.findall(statement) if date not in source_text]
    problems += [f"id {ident}" for ident in _ID_RE.findall(statement) if ident not in source_text]
    return list(dict.fromkeys(problems))


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "..."


def finalize_narrative(
    evidence: EvidenceSummary,
    risk_score: RiskScore,
    assessment: TypologyAssessment,
    text: NarrativeText,
    questions: list[str],
) -> SarNarrative:
    """Turn the model's raw answer into a checked draft.

    The model is allowed to be wrong; this is where that stops mattering. A sentence with a date,
    amount or id that appears nowhere in the evidence is dropped; ids that do not exist are
    removed; other doubts (an amount with no id, verdict-like wording) are kept but warned about.
    Whatever is changed is reported in `warnings`, never silently altered.
    """
    valid_ids = {evidence_id for ids in evidence.citations.values() for evidence_id in ids}
    valid_ids |= set(rule_engine_ids(risk_score))
    source_text = _source_text(evidence, risk_score, assessment)
    source_amounts = _amounts(source_text)
    warnings: list[str] = []

    def check(section: str, statements: list[NarrativeStatement]) -> list[NarrativeStatement]:
        kept: list[NarrativeStatement] = []
        for statement in statements:
            sentence = statement.text.strip()
            if not sentence:
                continue

            problems = _unsupported_details(sentence, source_text, source_amounts)
            if problems:
                warnings.append(
                    f"Dropped a {section} statement - {', '.join(problems)} not found in the evidence: "
                    f"'{_clip(sentence)}'"
                )
                continue

            cited = clean_ids(statement.evidence_ids, valid_ids)
            real_ids = [i for i in cited if i in valid_ids]
            invented = [i for i in cited if i not in valid_ids]
            if invented:
                shown = ", ".join(repr(i) for i in invented)
                warnings.append(f"Removed unknown evidence ids from a {section} statement: {shown}")
            named = [i for i in _RECORD_ID_RE.findall(sentence) if i in valid_ids]
            real_ids = list(dict.fromkeys([*real_ids, *named]))  # an id named in the text counts as cited
            if _MONEY_RE.search(sentence) and not real_ids:
                warnings.append(f"A {section} statement gives an amount but cites no evidence id: '{_clip(sentence)}'")
            warnings.extend(
                f"Verdict-like wording ('{phrase}') in a {section} statement - review it: '{_clip(sentence)}'"
                for phrase in verdict_words(sentence)
            )
            kept.append(NarrativeStatement(text=sentence, evidence_ids=real_ids))

        if not kept:
            warnings.append(f"The {section} is empty: every statement failed validation")
        return kept

    introduction = check("introduction", text.introduction)
    facts = check("body", text.facts)
    conclusion = check("conclusion", text.conclusion)

    # Completeness: a transaction the rule engine flagged should appear somewhere in the narrative.
    cited_anywhere = {i for part in (introduction, facts, conclusion) for s in part for i in s.evidence_ids}
    left_out = [i for i in rule_engine_ids(risk_score) if i not in cited_anywhere]
    if left_out:
        warnings.append(f"The rule engine flagged transactions that no statement cites: {', '.join(left_out)}")

    return SarNarrative(
        account_id=evidence.account_id,
        status="draft_for_human_review",
        introduction=introduction,
        facts=facts,
        conclusion=conclusion,
        typologies=[match.typology for match in assessment.matches],
        open_questions=questions,
        warnings=warnings,
    )


# ------------------------------------------------------------------------------ graph
def _prepare_node(questions_fn: QuestionsFn):
    def node(state: NarrativeState) -> dict:
        evidence, risk_score, assessment = state["evidence"], state["risk_score"], state["assessment"]
        if not should_draft(risk_score, assessment):
            return {
                "narrative": SarNarrative(
                    account_id=evidence.account_id,
                    status="not_drafted",
                    skip_reason="No typology matched and the rule engine score is below the medium tier, "
                    "so there is nothing to report.",
                )
            }
        try:
            questions = questions_fn([match.source_document for match in assessment.matches])
        except AMLCopilotError as exc:
            return {"error": str(exc)}
        return {"questions": questions}

    return node


def _write_node(write_fn: WriteFn):
    def node(state: NarrativeState) -> dict:
        evidence, risk_score, assessment = state["evidence"], state["risk_score"], state["assessment"]
        try:
            text = write_fn(build_narrative_prompt(evidence, risk_score, assessment))
        except AMLCopilotError as exc:  # e.g. the model's answer did not fit the schema
            return {"error": str(exc)}
        narrative = finalize_narrative(evidence, risk_score, assessment, text, state.get("questions", []))
        return {"narrative": narrative}

    return node


def build_narrative_graph(questions_fn: QuestionsFn, write_fn: WriteFn):
    """Compile the prepare -> write graph. Injected functions keep it testable offline."""
    graph = StateGraph(NarrativeState)
    graph.add_node("prepare", _prepare_node(questions_fn))
    graph.add_node("write", _write_node(write_fn))
    graph.set_entry_point("prepare")
    # `prepare` may already have produced the final answer (nothing to report) or an error.
    graph.add_conditional_edges("prepare", lambda s: END if s.get("error") or "narrative" in s else "write")
    graph.add_edge("write", END)
    return graph.compile()


# ---------------------------------------------------------------- the real wiring
def make_anthropic_writer(settings: Settings) -> WriteFn:
    """The real writer: Claude via langchain-anthropic, structured to NarrativeText."""
    chat = make_chat_model(settings, max_tokens=4096, model=settings.narrative_model or None)
    structured = chat.with_structured_output(NarrativeText)

    def write(prompt: str) -> NarrativeText:
        return invoke_structured(structured, prompt)

    return write


def write_narrative(
    evidence: EvidenceSummary,
    risk_score: RiskScore,
    assessment: TypologyAssessment,
    settings: Settings | None = None,
) -> NarrativeState:
    """Run the Narrative Agent against the real typology documents and Claude."""
    settings = settings or Settings()
    graph = build_narrative_graph(questions_fn=questions_for_sources, write_fn=make_anthropic_writer(settings))
    return graph.invoke({"evidence": evidence, "risk_score": risk_score, "assessment": assessment})


def main(argv: list[str]) -> None:
    """Usage: python -m aml_copilot.agents.narrative ACC-L-00000"""
    configure_logging_from_settings()
    if len(argv) != 2:
        sys.exit("usage: python -m aml_copilot.agents.narrative <account_id>")

    investigated = investigate(argv[1])
    if investigated.get("error"):
        sys.exit(f"error: {investigated['error']}")
    evidence, risk_score = investigated["summary"], investigated["risk_score"]

    typed = match_typology(evidence, risk_score)
    if typed.get("error"):
        sys.exit(f"error: {typed['error']}")
    assessment = typed["assessment"]

    result = write_narrative(evidence, risk_score, assessment)
    if result.get("error"):
        sys.exit(f"error: {result['error']}")
    print(result["narrative"].render())


if __name__ == "__main__":
    main(sys.argv)
