"""Evidence Agent: a three-node LangGraph that turns an account id into a cited summary.

    ingest --(ok)--> score --> summarize --> END
      |
      `--(error)-------------------------> END

`ingest` runs the existing pipeline (raw ingestion, integration, normalization, entity
resolution, network expansion) unchanged. `score` runs the Phase 3 detectors (structuring,
layering, risk score) - purely mechanical, no LLM. `summarize` asks Claude for a *factual,
cited* account summary: it may report the score as a fact from that rule engine, but it
never renders its own suspicion verdict on top of it - that stays the rule engine's job here,
and the Typology/RAG Agent's job downstream.

Dependencies (`ingest_fn`, `score_fn`, `summarize_fn`) are injected, so the graph is testable
without live credentials or an Anthropic API key - the same pattern as the ingestion sources.
"""

import re
from collections.abc import Callable

from langgraph.graph import END, StateGraph

from aml_copilot.agents.llm import invoke_structured, make_chat_model
from aml_copilot.agents.models import EvidenceSummary
from aml_copilot.agents.state import InvestigationState
from aml_copilot.config import Settings
from aml_copilot.detection import RiskScore, run_detection
from aml_copilot.errors import AMLCopilotError
from aml_copilot.ingestion import EvidenceBundle
from aml_copilot.ingestion.pipeline import DEFAULT_EXPANSION, ExpansionConfig, ingest

SYSTEM_PROMPT = """You are an AML evidence summarizer preparing material for a human \
investigator. Produce ONLY a factual, neutral summary of the evidence below - never a \
judgment about whether the account is suspicious. That determination belongs to a later \
step. You will also be shown output from a separate, deterministic rule-based detection \
system (a risk score and its reasons). Report that score as a fact attributed to the rule \
engine if it is present - for example "the rule-based detector assigned a score of X/100 \
(tier)" - and do not add your own suspicion assessment beyond restating what it found. \
Every sentence you write must be traceable to a specific transaction_id or case_id in the \
evidence; put those ids in the `citations` field, keyed by a short paraphrase of the claim \
they support. If evidence is thin or missing for a section, say so plainly in that section \
rather than speculating. Never state a fact that is not present below."""

SummarizeFn = Callable[[str], EvidenceSummary]
IngestFn = Callable[[str], EvidenceBundle]
ScoreFn = Callable[[EvidenceBundle], RiskScore]


def _fmt_amount(amount: object) -> str:
    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


def compute_stats(rows: list[dict]) -> dict:
    """Deterministic aggregates, computed in Python rather than trusted to the model."""
    if not rows:
        return {"count": 0}
    amounts = [float(r.get("amount", 0) or 0) for r in rows]
    by_type: dict[str, int] = {}
    for r in rows:
        t = str(r.get("type", "unknown"))
        by_type[t] = by_type.get(t, 0) + 1
    by_country: dict[str, int] = {}
    for r in rows:
        if r.get("country") is not None:
            by_country[str(r["country"])] = by_country.get(str(r["country"]), 0) + 1
    timestamps = sorted(str(r["timestamp"]) for r in rows if r.get("timestamp") is not None)
    return {
        "count": len(rows),
        "total_amount": sum(amounts),
        "min_amount": min(amounts),
        "max_amount": max(amounts),
        "by_type": by_type,
        "by_country": by_country,
        "date_range": (timestamps[0], timestamps[-1]) if timestamps else None,
    }


def _render_transactions(rows: list[dict], label: str, sample_limit: int = 15) -> str:
    if not rows:
        return f"{label}: none"
    stats = compute_stats(rows)
    header = (
        f"{label}: {stats['count']} rows, total {_fmt_amount(stats['total_amount'])}, "
        f"amount range {_fmt_amount(stats['min_amount'])}-{_fmt_amount(stats['max_amount'])}, "
        f"types {stats['by_type']}"
        + (f", countries {stats['by_country']}" if stats["by_country"] else "")
    )
    lines = [header]
    if stats["date_range"]:
        lines.append(f"  date range: {stats['date_range'][0]} to {stats['date_range'][1]}")
    sample = sorted(rows, key=lambda r: float(r.get("amount", 0) or 0), reverse=True)[:sample_limit]
    lines.append(f"  {len(sample)} largest rows (of {stats['count']}):")
    for r in sample:
        hop = f" hop={r['hop']}" if "hop" in r else ""
        where = "".join(f" {label}={r[key]}" for label, key in (("country", "country"), ("branch", "branch_id")) if r.get(key))
        lines.append(
            f"    {r.get('transaction_id', '?')}{hop}: {r.get('type')} {_fmt_amount(r.get('amount', 0))} "
            f"{r.get('account_id')} -> {r.get('counterparty_account')} at {r.get('timestamp')}{where}"
        )
    return "\n".join(lines)


def _render_risk_score(risk: RiskScore) -> str:
    header = (
        f"Automated detection score (from a separate, deterministic rule engine - "
        f"report as its finding, not your own judgment): {risk.score}/100 ({risk.tier})"
    )
    lines = [header]
    for r in risk.reasons:
        lines.append(f"  - {r}")
    if risk.structuring and risk.structuring.flagged:
        lines.append(f"    structuring evidence: transaction ids {risk.structuring.matched_transaction_ids}")
    if risk.layering and risk.layering.flagged:
        lines.append(f"    layering evidence: transaction ids {risk.layering.chain_transaction_ids}")
    return "\n".join(lines)


def build_evidence_prompt(bundle: EvidenceBundle, risk_score: RiskScore) -> str:
    """Render an EvidenceBundle and its risk score into the prompt the summarizer sees.
    No hidden fields: everything the model is told is in this string, so a test can
    assert against it."""
    parts = [SYSTEM_PROMPT, "", f"Account: {bundle.account_id}", ""]

    if bundle.profile:
        p = bundle.profile
        parts.append(
            f"KYC profile: name={p.name!r}, occupation={p.occupation!r}, "
            f"risk_rating={p.risk_rating!r}, is_pep={p.is_pep}, kyc_last_reviewed={p.kyc_last_reviewed}"
        )
    else:
        parts.append("KYC profile: none found in Salesforce")
    parts.append("")

    parts.append(_render_transactions(bundle.transactions, "Account's own transactions"))
    parts.append("")

    if bundle.related_accounts:
        chain = ", ".join(
            f"{a.account_id} (hop {a.hop})" for a in sorted(bundle.related_accounts, key=lambda a: a.hop)
        )
        parts.append(f"Related accounts found by network expansion: {chain}")
    else:
        parts.append("Related accounts found by network expansion: none")
    parts.append(_render_transactions(bundle.related_transactions, "Related-account transactions"))
    parts.append("")

    if bundle.case_history:
        lines = [f"Case history: {len(bundle.case_history)} prior case(s)"]
        for c in bundle.case_history[:10]:
            lines.append(
                f"    {c.case_id}: {c.case_type}, opened {c.opened_date}, "
                f"status={c.status}, disposition={c.disposition}"
            )
        if len(bundle.case_history) > 10:
            lines.append(f"    ... and {len(bundle.case_history) - 10} more")
        parts.append("\n".join(lines))
    else:
        parts.append("Case history: none")
    parts.append("")

    parts.append(_render_risk_score(risk_score))
    parts.append("")

    gaps = [*bundle.missing_sources, *bundle.warnings]
    parts.append("Data completeness notes: " + ("; ".join(gaps) if gaps else "none"))
    return "\n".join(parts)


def _is_empty(bundle: EvidenceBundle) -> bool:
    return not (bundle.transactions or bundle.related_transactions or bundle.profile or bundle.case_history)


def _empty_summary(bundle: EvidenceBundle, risk_score: RiskScore) -> EvidenceSummary:
    return EvidenceSummary(
        account_id=bundle.account_id,
        profile_summary="No KYC profile found.",
        transaction_summary="No transactions found for this account.",
        network_summary="No related accounts found by network expansion.",
        case_history_summary="No case history found.",
        detection_summary=f"Automated detection score: {risk_score.score}/100 ({risk_score.tier}).",
        data_completeness=[*bundle.missing_sources, *bundle.warnings],
    )


def _ingest_node(ingest_fn: IngestFn):
    def node(state: InvestigationState) -> dict:
        try:
            bundle = ingest_fn(state["account_id"])
        except AMLCopilotError as exc:
            return {"error": str(exc)}
        return {"bundle": bundle}

    return node


def _score_node(score_fn: ScoreFn):
    def node(state: InvestigationState) -> dict:
        return {"risk_score": score_fn(state["bundle"])}

    return node


def _summarize_node(summarize_fn: SummarizeFn):
    def node(state: InvestigationState) -> dict:
        bundle, risk_score = state["bundle"], state["risk_score"]
        if _is_empty(bundle):
            return {"summary": _empty_summary(bundle, risk_score)}
        try:
            summary = summarize_fn(build_evidence_prompt(bundle, risk_score))
        except AMLCopilotError as exc:  # e.g. the model's answer did not fit the schema
            return {"error": str(exc)}
        if summary.account_id != bundle.account_id:
            summary = summary.model_copy(update={"account_id": bundle.account_id})
        return {"summary": summary}

    return node


def build_evidence_graph(ingest_fn: IngestFn, score_fn: ScoreFn, summarize_fn: SummarizeFn):
    """Compile the ingest -> score -> summarize graph. Injected functions keep this
    testable without live credentials or an Anthropic API key."""
    graph = StateGraph(InvestigationState)
    graph.add_node("ingest", _ingest_node(ingest_fn))
    graph.add_node("score", _score_node(score_fn))
    graph.add_node("summarize", _summarize_node(summarize_fn))
    graph.set_entry_point("ingest")
    graph.add_conditional_edges("ingest", lambda s: END if s.get("error") else "score")
    graph.add_edge("score", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile()


_EVIDENCE_ID = re.compile(r"\b(?:TXN[A-Z0-9-]*\d|CASE-[A-Z]-\d+)\b")
_SECTIONS = ("profile_summary", "transaction_summary", "network_summary", "case_history_summary", "detection_summary")


def with_derived_citations(summary: EvidenceSummary) -> EvidenceSummary:
    """If the model left `citations` empty although its sentences name transaction / case ids, build the citations
    from the ids found in each section. The downstream agents rely on citations to know which ids are real; the ids are
    still checked against the rule-engine and evidence there, so this only restores what the model should have filled in."""
    if summary.citations:
        return summary
    derived = {}
    for section in _SECTIONS:
        ids = list(dict.fromkeys(_EVIDENCE_ID.findall(getattr(summary, section) or "")))
        if ids:
            derived[f"{section.removesuffix('_summary').replace('_', ' ')} (ids named in the summary)"] = ids
    return summary.model_copy(update={"citations": derived}) if derived else summary


def make_anthropic_summarizer(settings: Settings) -> SummarizeFn:
    """The real summarizer: the configured model (Gemini Flash by default), structured to EvidenceSummary."""
    structured = make_chat_model(settings).with_structured_output(EvidenceSummary)

    def summarize(prompt: str) -> EvidenceSummary:
        return with_derived_citations(invoke_structured(structured, prompt))

    return summarize


def investigate(
    account_id: str,
    settings: Settings | None = None,
    expansion: ExpansionConfig | None = DEFAULT_EXPANSION,
) -> InvestigationState:
    """Run the Evidence Agent against the real Databricks, Salesforce, Supabase and Claude."""
    settings = settings or Settings()
    graph = build_evidence_graph(
        ingest_fn=lambda acc: ingest(acc, settings, expansion),
        score_fn=run_detection,
        summarize_fn=make_anthropic_summarizer(settings),
    )
    return graph.invoke({"account_id": account_id})
