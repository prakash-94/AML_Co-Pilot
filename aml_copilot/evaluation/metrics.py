"""Grading a pipeline run against the expected answer, and summarizing many runs.

Pure functions over the orchestrator's result (`RunState`) - no model calls, so the numbers are
exactly reproducible from saved runs.
"""

from dataclasses import dataclass, field

from aml_copilot.evaluation.dataset import EvalCase


@dataclass
class CaseResult:
    case: EvalCase
    status: str
    tier: str | None
    matched_sources: frozenset
    drafted: bool
    sentences: int = 0
    supported: int = 0
    unsupported: int = 0
    contradicted: int = 0
    unverified: int = 0
    consistency_issues: int = 0
    faithfulness_passed: bool | None = None  # None = the check did not run for this case
    narrative_warnings: int = 0
    dropped_sentences: int = 0
    error: str = ""
    seconds: float = 0.0
    cost: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def tier_ok(self) -> bool:
        return self.tier == self.case.expected_tier

    @property
    def typology_ok(self) -> bool:
        return self.matched_sources == self.case.expected_sources

    @property
    def draft_ok(self) -> bool:
        return self.drafted == self.case.expects_draft


MIN_SUPPORTED_RATE = 0.90


def regressions(results: list["CaseResult"]) -> list[str]:
    """Plain-English reasons a scheduled run should be treated as a regression (empty list = healthy).

    Strict on purpose: the sample is tiny and the system is deterministic about tiers and typologies,
    so any miss is worth a human look.
    """
    problems = []
    for r in results:
        who = r.case.account_id
        if r.status == "failed":
            problems.append(f"{who}: the run failed ({r.error or 'no detail'})")
            continue
        if r.status == "incomplete":
            problems.append(f"{who}: evidence was incomplete (a data source was unavailable)")
        if not r.tier_ok:
            problems.append(f"{who}: risk tier was {r.tier}, expected {r.case.expected_tier}")
        if not r.typology_ok:
            problems.append(f"{who}: typology matches {sorted(r.matched_sources)}, expected {sorted(r.case.expected_sources)}")
        if not r.draft_ok:
            problems.append(f"{who}: draft {'was' if r.drafted else 'was not'} written, expected the opposite")
    # A flagged draft is normal (about 1 in 5 in the Phase 5 baseline) and goes to a human anyway, so one
    # does not fail the run; a drop in the share of supported sentences does (baseline 99% over 236 sentences; a 7-account canary is small, so the bar is 90%).
    checked = [r for r in results if r.drafted and r.faithfulness_passed is not None]
    sentences = sum(r.sentences for r in checked)
    if sentences and sum(r.supported for r in checked) / sentences < MIN_SUPPORTED_RATE:
        rate = sum(r.supported for r in checked) / sentences
        problems.append(f"only {rate:.0%} of draft sentences were supported by the evidence (minimum {MIN_SUPPORTED_RATE:.0%})")
    return problems


def score_case(case: EvalCase, state: dict, seconds: float = 0.0, cost: float = 0.0) -> CaseResult:
    """Grade one run of the orchestrator."""
    risk = state.get("risk_score")
    assessment = state.get("assessment")
    narrative = state.get("narrative")
    report = state.get("faithfulness")
    drafted = narrative is not None and narrative.status == "draft_for_human_review"

    result = CaseResult(
        case=case,
        status=state.get("status", "failed"),
        tier=risk.tier if risk else None,
        matched_sources=frozenset(m.source_document for m in assessment.matches) if assessment else frozenset(),
        drafted=drafted,
        error=state.get("error", ""),
        seconds=seconds,
        cost=cost,
    )
    if drafted:
        result.sentences = len(narrative.introduction) + len(narrative.facts) + len(narrative.conclusion)
        result.narrative_warnings = len(narrative.warnings)
        result.dropped_sentences = sum(1 for w in narrative.warnings if w.startswith("Dropped"))
    if report is not None:
        result.supported, result.unsupported = report.supported, report.unsupported
        result.contradicted, result.unverified = report.contradicted, report.unverified
        result.consistency_issues = len(report.consistency_issues)
        result.faithfulness_passed = report.passed
    return result


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(results: list[CaseResult]) -> dict:
    """Headline numbers over a set of graded runs. A rate is None when nothing could be measured."""
    ok = [r for r in results if r.status != "failed"]
    by_family: dict[str, dict] = {}
    for family in sorted({r.case.family for r in results}):
        group = [r for r in ok if r.case.family == family]
        by_family[family] = {
            "cases": sum(1 for r in results if r.case.family == family),
            "tier_accuracy": _rate(sum(r.tier_ok for r in group), len(group)),
            "typology_exact": _rate(sum(r.typology_ok for r in group), len(group)),
            "draft_ok": _rate(sum(r.draft_ok for r in group), len(group)),
        }

    true_pos = sum(len(r.matched_sources & r.case.expected_sources) for r in ok)
    false_pos = sum(len(r.matched_sources - r.case.expected_sources) for r in ok)
    false_neg = sum(len(r.case.expected_sources - r.matched_sources) for r in ok)

    normals = [r for r in ok if r.case.expected_tier == "low"]
    flagged_cases = [r for r in ok if r.case.expected_tier != "low"]
    drafts = [r for r in ok if r.drafted]
    checked = [r for r in drafts if r.faithfulness_passed is not None]
    sentences = sum(r.sentences for r in checked)

    return {
        "cases": len(results),
        "failed": len(results) - len(ok),
        "incomplete": sum(1 for r in results if r.status == "incomplete"),
        "tier_accuracy": _rate(sum(r.tier_ok for r in ok), len(ok)),
        "by_family": by_family,
        "typology_precision": _rate(true_pos, true_pos + false_pos),
        "typology_recall": _rate(true_pos, true_pos + false_neg),
        "false_alarm_rate": _rate(sum(1 for r in normals if not r.tier_ok or r.drafted), len(normals)),
        "missed_draft_rate": _rate(sum(1 for r in flagged_cases if not r.drafted), len(flagged_cases)),
        "drafts": len(drafts),
        "faithfulness_checked_drafts": len(checked),
        "faithfulness_pass_rate": _rate(sum(1 for r in checked if r.faithfulness_passed), len(checked)),
        "sentences_checked": sentences,
        "sentences_supported_rate": _rate(sum(r.supported for r in checked), sentences),
        "unsupported": sum(r.unsupported for r in checked),
        "contradicted": sum(r.contradicted for r in checked),
        "unverified": sum(r.unverified for r in checked),
        "consistency_issues": sum(r.consistency_issues for r in checked),
        "narrative_warnings_per_draft": _rate(sum(r.narrative_warnings for r in drafts), len(drafts)),
        "dropped_sentences": sum(r.dropped_sentences for r in drafts),
        "total_cost": sum(r.cost for r in results),
        "mean_seconds": _rate(round(sum(r.seconds for r in results)), len(results)),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def render_markdown(results: list[CaseResult], summary: dict, title: str) -> str:
    """A readable report: the headline numbers, then one line per account."""
    lines = [
        f"# {title}", "",
        (
            f"{summary['cases']} accounts, {summary['failed']} failed, {summary['incomplete']} incomplete. "
            f"Cost ${summary['total_cost']:.2f}" + (f", about {summary['mean_seconds']}s per account." if summary["mean_seconds"] else ".")
        ),
        "",
        "## Detection and matching", "",
        f"- Tier accuracy: **{_pct(summary['tier_accuracy'])}**",
        f"- Typology match precision: **{_pct(summary['typology_precision'])}**, recall: **{_pct(summary['typology_recall'])}**",
        f"- False alarms on normal accounts: **{_pct(summary['false_alarm_rate'])}**",
        f"- Flagged accounts that got no draft: **{_pct(summary['missed_draft_rate'])}**", "",
        "| Family | Accounts | Right tier | Exactly the right typologies | Right draft decision |", "|---|---|---|---|---|",
    ]
    for family, row in summary["by_family"].items():
        lines.append(
            f"| {family} | {row['cases']} | {_pct(row['tier_accuracy'])} | {_pct(row['typology_exact'])} | {_pct(row['draft_ok'])} |"
        )
    lines += [
        "", "## Narrative quality", "",
        f"- Drafts written: {summary['drafts']}; dropped for an invented figure/date/id: {summary['dropped_sentences']} sentences",
        f"- Narrative-agent warnings per draft: {summary['narrative_warnings_per_draft']}",
    ]
    if summary["faithfulness_checked_drafts"]:
        lines += [
            (
                f"- Faithfulness check ran on {summary['faithfulness_checked_drafts']} drafts: "
                f"**{_pct(summary['faithfulness_pass_rate'])}** passed outright; "
                f"**{_pct(summary['sentences_supported_rate'])}** of {summary['sentences_checked']} sentences supported "
                f"(unsupported {summary['unsupported']}, contradicted {summary['contradicted']}, "
                f"unverified {summary['unverified']}, consistency issues {summary['consistency_issues']})"
            ),
        ]
    lines += ["", "## Per account", "", "| Account | Expected | Got | Typologies | Draft | Faithful | Cost |", "|---|---|---|---|---|---|---|"]
    for r in results:
        faithful = "-" if r.faithfulness_passed is None else ("pass" if r.faithfulness_passed else f"{r.sentences - r.supported}/{r.sentences} flagged")
        got = r.tier or ("ERROR" if r.error else "-")
        matched = ", ".join(sorted(s.removesuffix('.md') for s in r.matched_sources)) or "none"
        lines.append(
            f"| {r.case.account_id} | {r.case.expected_tier} | {got}{'' if r.tier_ok else ' (!)'} | {matched}"
            f"{'' if r.typology_ok else ' (!)'} | {'yes' if r.drafted else 'no'}{'' if r.draft_ok else ' (!)'} | {faithful} | ${r.cost:.3f} |"
        )
    return "\n".join(lines) + "\n"
