"""Typology/RAG Agent: matches an account's evidence to documented laundering typologies.

    retrieve --(passages found)--> match --> END
        |
        `--(nothing to match / error)------> END

`retrieve` turns the Evidence Agent's summary into search queries and pulls the closest
typology passages out of the vector store - deterministic, no LLM. `match` shows those
passages and the evidence to Claude and asks which documented typologies, if any, the
evidence resembles. Everything the model says is then checked in plain Python
(`finalize_assessment`): a match that cites a document we never retrieved, or evidence ids
that do not exist, is dropped. The agent judges *resemblance to a documented pattern* - it
never decides guilt or recommends filing; that stays with the human investigator.

Dependencies (`search_fn`, `judge_fn`) are injected, so the graph is testable without the
embedding model, a built index, or an Anthropic API key.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, StateGraph

from aml_copilot.agents.checks import clean_ids, rule_engine_ids, verdict_words
from aml_copilot.agents.evidence import investigate
from aml_copilot.agents.llm import invoke_structured, make_chat_model
from aml_copilot.agents.models import (
    EvidenceSummary,
    PassageRef,
    RankedTypologyMatch,
    TypologyAssessment,
    TypologyJudgment,
)
from aml_copilot.config import Settings
from aml_copilot.detection import RiskScore
from aml_copilot.errors import AMLCopilotError
from aml_copilot.logging_config import configure_logging_from_settings
from aml_copilot.retrieval import RetrievedChunk, search
from aml_copilot.retrieval.store import default_embedder


@dataclass(frozen=True)
class TypologyConfig:
    top_k_per_query: int = 5  # passages fetched per query, before merging
    max_passages: int = 8  # passages actually shown to the model
    # Cosine distance cut-off: passages further away than this are never shown to the model.
    # Measured on live data (structuring, layering and normal accounts): the best passage for a
    # real match sat at 0.42-0.46, but the best passage for the NORMAL account sat at 0.49 - so
    # distance ranks passages well but cannot decide "match or no match"; the model does that.
    # This is therefore only a noise filter, kept loose on purpose.
    max_distance: float = 0.75


class TypologyState(TypedDict, total=False):
    evidence: EvidenceSummary
    risk_score: RiskScore
    passages: list[RetrievedChunk]
    assessment: TypologyAssessment
    error: str


SearchFn = Callable[[str, int], list[RetrievedChunk]]  # (query, top_k) -> nearest passages
JudgeFn = Callable[[str], TypologyJudgment]  # prompt -> the model's structured answer

SYSTEM_PROMPT = """You are an AML typology analyst preparing material for a human investigator. \
You are given (1) a factual evidence summary for one account, with the evidence ids available \
for citation, (2) the output of a separate deterministic rule engine, and (3) passages retrieved \
from a library of typology documents. Decide which documented typologies, if any, the evidence \
resembles.

Rules:
- Consider only typologies that appear in the retrieved passages. Set source_document to the \
exact filename shown with the passage.
- Judge resemblance to a documented pattern, never guilt. Do not state or imply that the account \
is laundering money, and do not recommend filing a report - that decision belongs to a human \
investigator.
- For each match, list the red flags from the passages that the evidence supports \
(matched_red_flags) and the red flags the evidence has no data to assess \
(unassessable_red_flags). Some typologies cannot be assessed from the available data at all \
(for example shell companies, crypto, real estate, hawala, cash-intensive businesses) - say so \
rather than guess.
- A red flag is matched only if the evidence states the specific fact it describes. A fact that \
is merely consistent with the flag, or that you would have to assume, belongs in \
unassessable_red_flags.
- A missing fact is never support. If the evidence does not say whether there was a business \
reason, a withdrawal, or an explanation, then it is unknown - do not describe it as absent, \
undocumented or unexplained.
- The account holder's occupation, risk rating and PEP status are context only. They are never \
a red flag on their own, and you must not say or imply that a profile does not fit legitimate \
activity.
- evidence_ids holds ids from the list provided and nothing else - no descriptions, and not the \
rule engine's findings (describe those in the rationale). Never invent an id. A match that cites \
no evidence will be discarded.
- Report a typology only if the evidence supports at least one of its red flags (matched_red_flags \
is not empty). Overlap in a single detail, such as a country the document names, is not a match; \
if every red flag is unassessable or unsupported, do not report that typology at all. A match with \
no supported red flag will be discarded.
- confidence: "high" only when several distinct red flags are directly supported by cited \
evidence; "medium" when some are; "low" when the resemblance is weak or mostly unassessable.
- If nothing in the passages fits the evidence, return no matches and explain in no_match_reason."""

# Which rule-based detector (a RiskScore field) stands behind which typology document.
# Typologies absent here have no detector - see typology_docs/README.md.
_DETECTOR_BY_DOCUMENT = {"structuring.md": "structuring", "rapid_wire_transfers.md": "layering"}

_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

# ---------------------------------------------------------------- retrieval (no LLM)
def build_queries(evidence: EvidenceSummary) -> list[str]:
    """One query per pattern-bearing field, rather than one long combined query.

    The embedding model only reads the first ~256 tokens of its input, so a long combined
    query would silently lose its tail. Case history is left out on purpose: it is noisy
    background (see the project notes) and says little about the *pattern* in the activity.
    """
    fields = [evidence.detection_summary, evidence.network_summary, evidence.transaction_summary]
    return [text.strip() for text in fields if text and text.strip()]


def merge_passages(results_per_query: list[list[RetrievedChunk]], config: TypologyConfig) -> list[RetrievedChunk]:
    """Combine the per-query results: one entry per passage (its best distance), irrelevant
    ones removed, closest first, capped at `max_passages`."""
    best: dict[str, RetrievedChunk] = {}
    for results in results_per_query:
        for result in results:
            if result.chunk.id not in best or result.distance < best[result.chunk.id].distance:
                best[result.chunk.id] = result

    close = [r for r in best.values() if r.distance <= config.max_distance]
    close.sort(key=lambda r: r.distance)
    return close[: config.max_passages]


# ---------------------------------------------------------------------------- prompt
def _where(chunk) -> str:
    if chunk.page:
        return f"page {chunk.page}"
    return f"section {chunk.heading}" if chunk.heading else "intro"


def build_typology_prompt(evidence: EvidenceSummary, risk_score: RiskScore, passages: list[RetrievedChunk]) -> str:
    """Everything the model is told is in this string, so a test can assert against it."""
    parts = [SYSTEM_PROMPT, "", f"Account: {evidence.account_id}", ""]
    parts.append(f"KYC profile: {evidence.profile_summary}")
    parts.append("")

    parts.append(f"Rule engine (separate, deterministic): score {risk_score.score}/100 ({risk_score.tier})")
    parts.extend(f"  - {reason}" for reason in risk_score.reasons)
    parts.append("")

    parts.append(f"Transactions: {evidence.transaction_summary}")
    parts.append(f"Related-account network: {evidence.network_summary}")
    parts.append("")

    parts.append("Evidence ids available for citation (claim -> ids):")
    if evidence.citations:
        parts.extend(f"  - {claim}: {', '.join(ids)}" for claim, ids in evidence.citations.items())
    else:
        parts.append("  (none)")
    rule_ids = rule_engine_ids(risk_score)
    if rule_ids:
        parts.append(f"Transaction ids flagged by the rule engine (also citable): {', '.join(rule_ids)}")
    parts.append("")

    parts.append("Retrieved typology passages (most similar first):")
    for number, passage in enumerate(passages, start=1):
        parts.append(
            f"[{number}] source={passage.chunk.source} {_where(passage.chunk)} distance={passage.distance:.2f}"
        )
        parts.append(passage.chunk.text)
        parts.append("")
    return "\n".join(parts)


# ------------------------------------------------- checking the model's answer (no LLM)
def _detector_corroborated(source_document: str, risk_score: RiskScore) -> bool | None:
    """Did the rule engine independently flag this typology? None if no detector exists."""
    signal_name = _DETECTOR_BY_DOCUMENT.get(source_document)
    if signal_name is None:
        return None
    signal = getattr(risk_score, signal_name)
    return signal.flagged if signal is not None else None


def _passage_ref(result: RetrievedChunk) -> PassageRef:
    chunk = result.chunk
    return PassageRef(
        source=chunk.source, heading=chunk.heading, page=chunk.page, distance=result.distance, text=chunk.text
    )


def _empty_assessment(
    evidence: EvidenceSummary, reason: str, passages: list[RetrievedChunk] | None = None
) -> TypologyAssessment:
    return TypologyAssessment(
        account_id=evidence.account_id,
        no_match_reason=reason,
        passages=[_passage_ref(p) for p in passages or []],
    )


def finalize_assessment(
    evidence: EvidenceSummary,
    risk_score: RiskScore,
    passages: list[RetrievedChunk],
    judgment: TypologyJudgment,
) -> TypologyAssessment:
    """Turn the model's raw answer into a validated, ranked assessment.

    The model is allowed to be wrong; this is where that stops mattering. A match survives only
    if it is based on a document we actually retrieved and cites at least one evidence id that
    really exists. Whatever is dropped is reported in `warnings`, never silently discarded.
    """
    valid_sources = {p.chunk.source for p in passages}
    valid_ids = {evidence_id for ids in evidence.citations.values() for evidence_id in ids}
    valid_ids |= set(rule_engine_ids(risk_score))
    warnings: list[str] = []
    kept: list[RankedTypologyMatch] = []

    for match in judgment.matches:
        if match.source_document not in valid_sources:
            warnings.append(
                f"Dropped match '{match.typology}': '{match.source_document}' was not among the retrieved passages"
            )
            continue

        cited = clean_ids(match.evidence_ids, valid_ids)
        real_ids = [i for i in cited if i in valid_ids]
        invented_ids = [i for i in cited if i not in valid_ids]
        if invented_ids:
            # repr() so odd values (stray spaces, joined strings) are visible in the warning
            shown = ", ".join(repr(i) for i in invented_ids)
            warnings.append(f"Removed unknown evidence ids from '{match.typology}': {shown}")
        if not real_ids:
            warnings.append(f"Dropped match '{match.typology}': it cites no valid evidence id")
            continue
        if not match.matched_red_flags:
            # Found by the evaluation: a low-confidence "match" that cited no supported red flag at
            # all (one shared detail - a country - and every other flag unassessable). That is not
            # resemblance, and it cost the evaluation a point of typology precision.
            warnings.append(f"Dropped match '{match.typology}': none of its red flags is supported by the evidence")
            continue

        for phrase in verdict_words(match.rationale):
            warnings.append(f"Verdict-like wording ('{phrase}') in the rationale for '{match.typology}' - review it")

        data = {
            **match.model_dump(),
            "evidence_ids": real_ids,
            "detector_corroborated": _detector_corroborated(match.source_document, risk_score),
        }
        kept.append(RankedTypologyMatch(**data))

    kept.sort(key=lambda m: _CONFIDENCE_RANK[m.confidence])  # stable: ties keep the model's order

    unique: list[RankedTypologyMatch] = []
    seen: set[str] = set()
    for match in kept:
        if match.source_document in seen:
            warnings.append(f"Dropped duplicate match for {match.source_document}")
            continue
        seen.add(match.source_document)
        unique.append(match)

    no_match_reason = ""
    if not unique:
        no_match_reason = judgment.no_match_reason or (
            "Every proposed match failed validation - see warnings."
            if judgment.matches
            else "No documented typology matched the evidence."
        )

    return TypologyAssessment(
        account_id=evidence.account_id,
        matches=unique,
        no_match_reason=no_match_reason,
        passages=[_passage_ref(p) for p in passages],
        warnings=warnings,
    )


# ------------------------------------------------------------------------------ graph
def _retrieve_node(search_fn: SearchFn, config: TypologyConfig):
    def node(state: TypologyState) -> dict:
        evidence = state["evidence"]
        if not evidence.citations:
            return {"assessment": _empty_assessment(evidence, "No evidence available to match.")}
        try:
            per_query = [search_fn(query, config.top_k_per_query) for query in build_queries(evidence)]
        except AMLCopilotError as exc:
            return {"error": str(exc)}

        passages = merge_passages(per_query, config)
        if not passages:
            return {
                "assessment": _empty_assessment(evidence, "No typology passage was similar enough to the evidence.")
            }
        return {"passages": passages}

    return node


def _match_node(judge_fn: JudgeFn):
    def node(state: TypologyState) -> dict:
        evidence, risk_score, passages = state["evidence"], state["risk_score"], state["passages"]
        try:
            judgment = judge_fn(build_typology_prompt(evidence, risk_score, passages))
        except AMLCopilotError as exc:  # e.g. the model's answer did not fit the schema
            return {"error": str(exc)}
        return {"assessment": finalize_assessment(evidence, risk_score, passages, judgment)}

    return node


def build_typology_graph(search_fn: SearchFn, judge_fn: JudgeFn, config: TypologyConfig | None = None):
    """Compile the retrieve -> match graph. Injected functions keep it testable offline."""
    config = config or TypologyConfig()
    graph = StateGraph(TypologyState)
    graph.add_node("retrieve", _retrieve_node(search_fn, config))
    graph.add_node("match", _match_node(judge_fn))
    graph.set_entry_point("retrieve")
    # `retrieve` may already have produced a final answer (or an error) - then skip the LLM.
    graph.add_conditional_edges("retrieve", lambda s: END if s.get("error") or "assessment" in s else "match")
    graph.add_edge("match", END)
    return graph.compile()


# ---------------------------------------------------------------- the real wiring
def make_anthropic_judge(settings: Settings) -> JudgeFn:
    """The real judge: Claude via langchain-anthropic, structured to TypologyJudgment."""
    # max_tokens: several matches with red-flag lists can exceed the default cap
    chat = make_chat_model(settings, max_tokens=4096, model=settings.typology_model or None)
    structured = chat.with_structured_output(TypologyJudgment)

    def judge(prompt: str) -> TypologyJudgment:
        return invoke_structured(structured, prompt)

    return judge


def match_typology(
    evidence: EvidenceSummary,
    risk_score: RiskScore,
    settings: Settings | None = None,
    config: TypologyConfig | None = None,
) -> TypologyState:
    """Run the Typology/RAG Agent against the real vector index and Claude."""
    settings = settings or Settings()
    judge_fn = make_anthropic_judge(settings)  # fail fast on a missing API key...
    embed_fn = default_embedder()  # ...before loading the local model, once for every query
    graph = build_typology_graph(
        search_fn=lambda query, k: search(query, top_k=k, embed_fn=embed_fn, doc_type="typology"),
        judge_fn=judge_fn,
        config=config,
    )
    return graph.invoke({"evidence": evidence, "risk_score": risk_score})


def main(argv: list[str]) -> None:
    """Usage: python -m aml_copilot.agents.typology ACC-L-00000"""
    configure_logging_from_settings()
    if len(argv) != 2:
        sys.exit("usage: python -m aml_copilot.agents.typology <account_id>")

    investigated = investigate(argv[1])
    if investigated.get("error"):
        sys.exit(f"error: {investigated['error']}")

    result = match_typology(investigated["summary"], investigated["risk_score"])
    if result.get("error"):
        sys.exit(f"error: {result['error']}")

    assessment = result["assessment"]
    print(f"Risk score: {investigated['risk_score'].score}/100 ({investigated['risk_score'].tier})")
    print(f"Passages shown to the model: {len(assessment.passages)}")
    for passage in assessment.passages:
        where = f"p.{passage.page}" if passage.page else (passage.heading or "")
        print(f"  [{passage.distance:.3f}] {passage.source} {where}")
    print()
    print(assessment.model_dump_json(indent=2, exclude={"passages"}))


if __name__ == "__main__":
    main(sys.argv)
