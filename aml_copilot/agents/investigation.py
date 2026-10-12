"""Investigation orchestrator: one account id in, one checked narrative draft out.

    evidence --(ok)--> typology --(ok)--> narrative --(ok)--> faithfulness --> conclude --> END
        |                 |                   |                                       ^
        `-----(error)-----+-----(error)-------+---------------------------------------'
                          (any error skips straight to conclude)

The three agents already exist as graphs of their own (`evidence.py`, `typology.py`,
`narrative.py`). This graph adds no analysis and calls no model itself: it fixes the order, hands
each agent's output to the next, stops at the first failure, and works out what the run *means*.

That last part matters. If a source system was down, the evidence is thin, nothing matches, and
the Narrative Agent will say there is nothing to report - which would look exactly like a clean
account. So `conclude` never calls that outcome "no action" unless the evidence was complete: an
outage is reported as "incomplete", never as "all clear". The run ends at a draft for a human
investigator; it never decides that a report should be filed.

Dependencies (`evidence_fn`, `typology_fn`, `narrative_fn`) are injected, so the graph is testable
without databases, an index or an API key.
"""

import sys
import time
from collections.abc import Callable
from typing import Literal, TypedDict

from langgraph.graph import END, StateGraph

from aml_copilot.agents.evidence import investigate
from aml_copilot.agents.models import EvidenceSummary, SarNarrative, TypologyAssessment
from aml_copilot.agents.narrative import NarrativeState, write_narrative
from aml_copilot.agents.state import InvestigationState
from aml_copilot.agents.typology import TypologyState, match_typology
from aml_copilot.config import Settings
from aml_copilot.detection import RiskScore
from aml_copilot.guardrails import FaithfulnessReport, run_faithfulness
from aml_copilot.guardrails.faithfulness import FaithfulnessState
from aml_copilot.ingestion import EvidenceBundle
from aml_copilot.logging_config import configure_logging_from_settings

# draft_ready: a narrative was drafted and now needs a human.
# no_action:   the evidence was complete and there is nothing to report.
# incomplete:  no draft, but a source was unavailable - this is NOT an all-clear.
# failed:      a step raised an error; see `failed_step` and `error`.
RunStatus = Literal["draft_ready", "no_action", "incomplete", "failed"]


class RunState(TypedDict, total=False):
    account_id: str
    bundle: EvidenceBundle
    evidence: EvidenceSummary
    risk_score: RiskScore
    assessment: TypologyAssessment
    narrative: SarNarrative
    faithfulness: FaithfulnessReport
    faithfulness_error: str  # the check could not run; the draft itself is unaffected
    steps_completed: list[str]
    failed_step: str
    error: str
    # filled in by `conclude`
    status: RunStatus
    status_detail: str
    missing_sources: list[str]
    warnings: list[str]


EvidenceFn = Callable[[str], InvestigationState]
TypologyFn = Callable[[EvidenceSummary, RiskScore], TypologyState]
NarrativeFn = Callable[[EvidenceSummary, RiskScore, TypologyAssessment], NarrativeState]
FaithfulnessFn = Callable[[EvidenceSummary, RiskScore, TypologyAssessment, SarNarrative], FaithfulnessState]


# ----------------------------------------------------------- what the run means (no LLM)
def decide_outcome(state: RunState) -> tuple[RunStatus, str]:
    """The run's status and a one-sentence explanation, worked out from what actually happened."""
    if state.get("error"):
        return "failed", f"The {state.get('failed_step', 'unknown')} step failed: {state['error']}"

    bundle = state.get("bundle")
    missing = bundle.missing_sources if bundle else []
    narrative = state.get("narrative")

    if narrative is not None and narrative.status == "draft_for_human_review":
        detail = "A narrative draft is ready for human review."
        if missing:
            detail += f" The evidence is incomplete: {', '.join(missing)} could not be queried."
        report = state.get("faithfulness")
        if report is not None and not report.passed:
            flagged = len(report.flagged) + len(report.consistency_issues)
            detail += f" The faithfulness check flagged {flagged} item(s) for attention."
        elif state.get("faithfulness_error"):
            detail += " The faithfulness check could not run, so the draft has not been fact-checked."
        return "draft_ready", detail

    if missing:
        return "incomplete", (
            f"No narrative was drafted, but {', '.join(missing)} could not be queried, so this is NOT "
            "evidence that there is nothing to report. Re-run once the source is available."
        )

    reason = narrative.skip_reason if narrative is not None else "No narrative was produced."
    return "no_action", reason


def collect_warnings(state: RunState) -> list[str]:
    """Every agent's warnings in one list, each tagged with the stage it came from."""
    warnings: list[str] = []
    if bundle := state.get("bundle"):
        warnings.extend(f"[ingestion] {w}" for w in bundle.warnings)
    if assessment := state.get("assessment"):
        warnings.extend(f"[typology] {w}" for w in assessment.warnings)
    if narrative := state.get("narrative"):
        warnings.extend(f"[narrative] {w}" for w in narrative.warnings)
    if report := state.get("faithfulness"):
        warnings.extend(
            f"[faithfulness] sentence {check.number} is {check.verdict}: '{check.text[:80]}' - {check.reason}"
            for check in report.flagged
        )
        warnings.extend(f"[consistency] {issue}" for issue in report.consistency_issues)
    if error := state.get("faithfulness_error"):
        warnings.append(f"[faithfulness] the check could not run: {error}")
    return warnings


# ------------------------------------------------------------------------------ graph
def _completed(state: RunState, step: str) -> list[str]:
    return [*state.get("steps_completed", []), step]


def _evidence_node(evidence_fn: EvidenceFn):
    def node(state: RunState) -> dict:
        result = evidence_fn(state["account_id"])
        if result.get("error"):
            return {"error": result["error"], "failed_step": "evidence"}
        return {
            "bundle": result["bundle"],
            "evidence": result["summary"],
            "risk_score": result["risk_score"],
            "steps_completed": _completed(state, "evidence"),
        }

    return node


def _typology_node(typology_fn: TypologyFn):
    def node(state: RunState) -> dict:
        result = typology_fn(state["evidence"], state["risk_score"])
        if result.get("error"):
            return {"error": result["error"], "failed_step": "typology"}
        return {"assessment": result["assessment"], "steps_completed": _completed(state, "typology")}

    return node


def _narrative_node(narrative_fn: NarrativeFn):
    def node(state: RunState) -> dict:
        result = narrative_fn(state["evidence"], state["risk_score"], state["assessment"])
        if result.get("error"):
            return {"error": result["error"], "failed_step": "narrative"}
        return {"narrative": result["narrative"], "steps_completed": _completed(state, "narrative")}

    return node


def _faithfulness_node(faithfulness_fn: FaithfulnessFn):
    def node(state: RunState) -> dict:
        narrative = state["narrative"]
        if narrative.status != "draft_for_human_review":
            return {}  # nothing was drafted, so there is nothing to fact-check
        result = faithfulness_fn(state["evidence"], state["risk_score"], state["assessment"], narrative)
        if result.get("error"):
            return {"faithfulness_error": result["error"]}  # not fatal: the draft is still good to review
        return {"faithfulness": result["report"], "steps_completed": _completed(state, "faithfulness")}

    return node


def _conclude_node():
    def node(state: RunState) -> dict:
        status, detail = decide_outcome(state)
        bundle = state.get("bundle")
        return {
            "status": status,
            "status_detail": detail,
            "missing_sources": list(bundle.missing_sources) if bundle else [],
            "warnings": collect_warnings(state),
        }

    return node


def build_investigation_graph(
    evidence_fn: EvidenceFn,
    typology_fn: TypologyFn,
    narrative_fn: NarrativeFn,
    faithfulness_fn: FaithfulnessFn | None = None,
):
    """Compile the evidence -> typology -> narrative -> (faithfulness) -> conclude graph.

    Without a `faithfulness_fn` the fact-check step is left out of the graph altogether.
    """
    graph = StateGraph(RunState)
    graph.add_node("evidence", _evidence_node(evidence_fn))
    graph.add_node("typology", _typology_node(typology_fn))
    graph.add_node("narrative", _narrative_node(narrative_fn))
    graph.add_node("conclude", _conclude_node())
    graph.set_entry_point("evidence")
    # The first failure skips the remaining agents and goes straight to `conclude`.
    graph.add_conditional_edges("evidence", lambda s: "conclude" if s.get("error") else "typology")
    graph.add_conditional_edges("typology", lambda s: "conclude" if s.get("error") else "narrative")
    if faithfulness_fn is None:
        graph.add_edge("narrative", "conclude")
    else:
        graph.add_node("faithfulness", _faithfulness_node(faithfulness_fn))
        graph.add_conditional_edges("narrative", lambda s: "conclude" if s.get("error") else "faithfulness")
        graph.add_edge("faithfulness", "conclude")
    graph.add_edge("conclude", END)
    return graph.compile()


# ---------------------------------------------------------------- the real wiring
def run_investigation(
    account_id: str,
    settings: Settings | None = None,
    check_faithfulness: bool = True,
    observer: Callable[[str, float], None] | None = None,
) -> RunState:
    """Run the whole pipeline against the real databases, vector index and Claude.

    The faithfulness check costs one more model call (about a cent); pass
    `check_faithfulness=False` to leave it out. `observer(step, seconds)`, if given, is called after
    each step with how long it took (the service uses it for the audit trail).
    """
    settings = settings or Settings()

    def timed(step: str, fn):
        def wrapper(*args):
            started = time.monotonic()
            try:
                return fn(*args)
            finally:
                if observer is not None:
                    observer(step, round(time.monotonic() - started, 1))

        return wrapper

    faithfulness_fn = None
    if check_faithfulness:

        def faithfulness_fn(evidence, risk, assessment, narrative):
            return run_faithfulness(evidence, risk, assessment, narrative, settings)

        faithfulness_fn = timed("faithfulness", faithfulness_fn)

    graph = build_investigation_graph(
        evidence_fn=timed("evidence", lambda account: investigate(account, settings)),
        typology_fn=timed("typology", lambda evidence, risk: match_typology(evidence, risk, settings)),
        narrative_fn=timed(
            "narrative", lambda evidence, risk, assessment: write_narrative(evidence, risk, assessment, settings)
        ),
        faithfulness_fn=faithfulness_fn,
    )
    return graph.invoke({"account_id": account_id})


_HEADLINES = {
    "draft_ready": "DRAFT READY FOR HUMAN REVIEW",
    "no_action": "NO ACTION NEEDED",
    "incomplete": "INCOMPLETE - EVIDENCE MISSING",
    "failed": "FAILED",
}
_AGREEMENT = {
    True: "the rule engine agrees",
    False: "the rule engine did not flag it",
    None: "no rule-based detector exists for it",
}


def _faithfulness_line(state: RunState) -> str:
    report = state.get("faithfulness")
    if report is not None:
        total = len(report.checks)
        if report.passed:
            return f"Faithfulness check: PASSED ({report.supported} of {total} sentences supported by the evidence)"
        flagged = len(report.flagged) + len(report.consistency_issues)
        return f"Faithfulness check: NEEDS ATTENTION ({flagged} item(s) flagged; {report.supported} of {total} supported)"
    if state.get("faithfulness_error"):
        return "Faithfulness check: COULD NOT RUN"
    return "Faithfulness check: not run"


def render_run(state: RunState) -> str:
    """Plain-text report of a run, for a person (or the command line) to read."""
    lines = [f"Investigation of {state['account_id']}: {_HEADLINES[state['status']]}", f"  {state['status_detail']}", ""]
    lines.append("Steps completed: " + (" -> ".join(state.get("steps_completed", [])) or "none"))
    if risk := state.get("risk_score"):
        lines.append(f"Risk score: {risk.score}/100 ({risk.tier})")
    if assessment := state.get("assessment"):
        if assessment.matches:
            lines.extend(
                f"Typology match: {m.typology} - {m.confidence} confidence ({_AGREEMENT[m.detector_corroborated]})"
                for m in assessment.matches
            )
        else:
            lines.append("Typology match: none")
    lines.append("Sources unavailable: " + (", ".join(state["missing_sources"]) or "none"))
    lines.append(_faithfulness_line(state))

    narrative = state.get("narrative")
    if narrative is not None and narrative.status == "draft_for_human_review":
        lines += ["", narrative.render()]  # the draft's own warnings are printed with it

    report = state.get("faithfulness")
    if report is not None and (report.flagged or report.consistency_issues):
        lines += ["", "Faithfulness check - sentences for the investigator to look at"]
        lines.extend(f"  {c.number}. {c.verdict.upper()}: {c.text}\n       {c.reason}" for c in report.flagged)
        lines.extend(f"  - Consistency: {issue}" for issue in report.consistency_issues)

    shown_above = ("[narrative]", "[faithfulness]", "[consistency]")
    other = [w for w in state.get("warnings", []) if not w.startswith(shown_above)]
    if other:
        lines += ["", "Other warnings"]
        lines.extend(f"  - {w}" for w in other)
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    """Usage: python -m aml_copilot.agents.investigation ACC-L-00000 [--no-faithfulness]"""
    configure_logging_from_settings()
    args = [a for a in argv[1:] if a != "--no-faithfulness"]
    if len(args) != 1:
        sys.exit("usage: python -m aml_copilot.agents.investigation <account_id> [--no-faithfulness]")
    state = run_investigation(args[0], check_faithfulness="--no-faithfulness" not in argv)
    print(render_run(state))
    if state["status"] == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
