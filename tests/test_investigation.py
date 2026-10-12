"""Investigation orchestrator: order, hand-offs, failure handling, and what a run means."""

import pytest

from aml_copilot.agents.investigation import (
    build_investigation_graph,
    collect_warnings,
    decide_outcome,
    render_run,
    run_investigation,
)
from aml_copilot.agents.models import (
    EvidenceSummary,
    NarrativeStatement,
    RankedTypologyMatch,
    SarNarrative,
    TypologyAssessment,
)
from aml_copilot.detection import LayeringSignal, RiskScore
from aml_copilot.guardrails import FaithfulnessReport, StatementCheck
from aml_copilot.ingestion import EvidenceBundle

ACCOUNT = "ACC-L-00000"
HOPS = ["TXN095042", "TXN095043", "TXN095044"]


# ----------------------------------------------------------------------- test data
def summary():
    return EvidenceSummary(
        account_id=ACCOUNT,
        profile_summary="p",
        transaction_summary="t",
        network_summary="n",
        case_history_summary="c",
        detection_summary="d",
        citations={"a wire": HOPS},
    )


def risk(tier="medium"):
    flagged = tier != "low"
    return RiskScore(
        score=50 if flagged else 0,
        tier=tier,
        layering=LayeringSignal(flagged=flagged, chain_transaction_ids=HOPS if flagged else []),
    )


def assessment(matched=True, warnings=()):
    matches = []
    if matched:
        matches = [
            RankedTypologyMatch(
                typology="Rapid wire transfers (layering)",
                source_document="rapid_wire_transfers.md",
                confidence="high",
                rationale="r",
                evidence_ids=HOPS,
                detector_corroborated=True,
            )
        ]
    return TypologyAssessment(account_id=ACCOUNT, matches=matches, warnings=list(warnings))


def drafted(warnings=()):
    return SarNarrative(
        account_id=ACCOUNT,
        status="draft_for_human_review",
        introduction=[NarrativeStatement(text="Intro.")],
        facts=[NarrativeStatement(text="A fact.", evidence_ids=HOPS)],
        conclusion=[NarrativeStatement(text="Done.")],
        warnings=list(warnings),
    )


def not_drafted():
    return SarNarrative(account_id=ACCOUNT, status="not_drafted", skip_reason="Nothing to report.")


class Fakes:
    """Stand-ins for the three agents that record what they were called with."""

    def __init__(self, *, bundle=None, risk_score=None, asm=None, narrative=None, fail=None):
        self.calls = []
        self.bundle = bundle or EvidenceBundle(account_id=ACCOUNT)
        self.risk_score = risk_score or risk()
        self.asm = asm or assessment()
        self.narrative = narrative or drafted()
        self.fail = fail  # name of the step that should return an error

    def evidence(self, account_id):
        self.calls.append(("evidence", account_id))
        if self.fail == "evidence":
            return {"error": "databricks exploded"}
        return {"bundle": self.bundle, "summary": summary(), "risk_score": self.risk_score}

    def typology(self, evidence, risk_score):
        self.calls.append(("typology", evidence, risk_score))
        if self.fail == "typology":
            return {"error": "bad shape"}
        return {"assessment": self.asm}

    def narrative_step(self, evidence, risk_score, asm):
        self.calls.append(("narrative", evidence, risk_score, asm))
        if self.fail == "narrative":
            return {"error": "schema mismatch"}
        return {"narrative": self.narrative}

    def run(self):
        graph = build_investigation_graph(self.evidence, self.typology, self.narrative_step)
        return graph.invoke({"account_id": ACCOUNT})


# ------------------------------------------------------------------ the happy path
def test_the_agents_run_in_order_and_each_receives_the_previous_ones_output():
    fakes = Fakes()
    state = fakes.run()

    assert [call[0] for call in fakes.calls] == ["evidence", "typology", "narrative"]
    assert fakes.calls[0][1] == ACCOUNT
    assert fakes.calls[1][1].account_id == ACCOUNT and fakes.calls[1][2] is fakes.risk_score
    assert fakes.calls[2][3] is fakes.asm  # the narrative got the typology agent's own assessment
    assert state["steps_completed"] == ["evidence", "typology", "narrative"]
    assert state["status"] == "draft_ready" and "ready for human review" in state["status_detail"]
    assert state["narrative"].status == "draft_for_human_review"


# --------------------------------------------------------------- failures stop the run
@pytest.mark.parametrize(
    ("failing_step", "expected_calls", "kept"),
    [
        ("evidence", ["evidence"], []),
        ("typology", ["evidence", "typology"], ["bundle", "evidence", "risk_score"]),
        ("narrative", ["evidence", "typology", "narrative"], ["bundle", "evidence", "risk_score", "assessment"]),
    ],
)
def test_a_failing_step_stops_the_run_and_keeps_everything_produced_so_far(failing_step, expected_calls, kept):
    fakes = Fakes(fail=failing_step)
    state = fakes.run()

    assert [call[0] for call in fakes.calls] == expected_calls  # later agents are never called
    assert state["status"] == "failed" and state["failed_step"] == failing_step
    assert f"The {failing_step} step failed" in state["status_detail"]
    assert all(key in state for key in kept)
    assert "narrative" not in state


# ------------------------------------------------- what the run means (the outage rule)
def test_a_clean_account_with_complete_evidence_is_no_action():
    state = Fakes(risk_score=risk("low"), asm=assessment(matched=False), narrative=not_drafted()).run()
    assert state["status"] == "no_action" and state["status_detail"] == "Nothing to report."


def test_an_outage_with_nothing_drafted_is_incomplete_never_an_all_clear():
    bundle = EvidenceBundle(account_id=ACCOUNT, missing_sources=["databricks"])
    state = Fakes(bundle=bundle, risk_score=risk("low"), asm=assessment(matched=False), narrative=not_drafted()).run()
    assert state["status"] == "incomplete"
    assert "databricks" in state["status_detail"] and "NOT evidence that there is nothing to report" in state["status_detail"]
    assert state["missing_sources"] == ["databricks"]


def test_a_draft_made_from_partial_evidence_is_flagged_as_incomplete():
    bundle = EvidenceBundle(account_id=ACCOUNT, missing_sources=["supabase"])
    state = Fakes(bundle=bundle).run()
    assert state["status"] == "draft_ready"
    assert "evidence is incomplete: supabase could not be queried" in state["status_detail"]
    assert state["missing_sources"] == ["supabase"]


def test_decide_outcome_reports_the_failing_step_even_without_a_bundle():
    status, detail = decide_outcome({"error": "boom", "failed_step": "evidence"})
    assert status == "failed" and detail == "The evidence step failed: boom"


# ---------------------------------------------------------------------- the warnings
def test_every_stages_warnings_are_collected_and_tagged():
    bundle = EvidenceBundle(account_id=ACCOUNT, warnings=["No Salesforce KYC profile found"])
    state = Fakes(bundle=bundle, asm=assessment(warnings=["Dropped duplicate match"]), narrative=drafted(["Amount with no id"])).run()
    assert state["warnings"] == [
        "[ingestion] No Salesforce KYC profile found",
        "[typology] Dropped duplicate match",
        "[narrative] Amount with no id",
    ]
    assert collect_warnings({}) == []


# --------------------------------------------------------------------- the report
def test_the_report_shows_status_steps_match_draft_and_warnings_once():
    state = Fakes(narrative=drafted(["Amount with no id"]), asm=assessment(warnings=["Dropped duplicate match"])).run()
    text = render_run(state)
    assert text.startswith(f"Investigation of {ACCOUNT}: DRAFT READY FOR HUMAN REVIEW")
    assert "Steps completed: evidence -> typology -> narrative" in text
    assert "Risk score: 50/100 (medium)" in text
    assert "Typology match: Rapid wire transfers (layering) - high confidence (the rule engine agrees)" in text
    assert "Sources unavailable: none" in text
    assert "SAR NARRATIVE DRAFT" in text
    assert text.count("Amount with no id") == 1  # printed with the draft, not repeated below it
    assert "[typology] Dropped duplicate match" in text


def test_the_report_for_a_failed_run_has_no_draft_section():
    text = render_run(Fakes(fail="typology").run())
    assert "FAILED" in text and "The typology step failed: bad shape" in text
    assert "Steps completed: evidence" in text and "SAR NARRATIVE DRAFT" not in text


def test_the_report_for_an_outage_says_which_source_is_down():
    bundle = EvidenceBundle(account_id=ACCOUNT, missing_sources=["databricks", "salesforce"])
    text = render_run(Fakes(bundle=bundle, risk_score=risk("low"), asm=assessment(False), narrative=not_drafted()).run())
    assert "INCOMPLETE - EVIDENCE MISSING" in text and "Sources unavailable: databricks, salesforce" in text


# ---------------------------------------------------------------- the real wiring
def test_run_investigation_passes_the_same_settings_to_every_real_agent(monkeypatch):
    from aml_copilot.config import Settings

    settings = Settings(_env_file=None)
    seen = {}
    fakes = Fakes()

    def fake_investigate(account_id, given_settings):
        seen["evidence"] = given_settings
        return fakes.evidence(account_id)

    def fake_match(evidence, risk_score, given_settings):
        seen["typology"] = given_settings
        return fakes.typology(evidence, risk_score)

    def fake_write(evidence, risk_score, asm, given_settings):
        seen["narrative"] = given_settings
        return fakes.narrative_step(evidence, risk_score, asm)

    def fake_faithfulness(evidence, risk_score, asm, narr, given_settings):
        seen["faithfulness"] = given_settings
        return {"report": report()}

    monkeypatch.setattr("aml_copilot.agents.investigation.investigate", fake_investigate)
    monkeypatch.setattr("aml_copilot.agents.investigation.match_typology", fake_match)
    monkeypatch.setattr("aml_copilot.agents.investigation.write_narrative", fake_write)
    monkeypatch.setattr("aml_copilot.agents.investigation.run_faithfulness", fake_faithfulness)

    state = run_investigation(ACCOUNT, settings)
    assert seen == {"evidence": settings, "typology": settings, "narrative": settings, "faithfulness": settings}
    assert state["status"] == "draft_ready" and state["steps_completed"][-1] == "faithfulness"


def test_the_faithfulness_step_can_be_switched_off_and_then_never_runs(monkeypatch):
    from aml_copilot.config import Settings

    fakes = Fakes()
    monkeypatch.setattr("aml_copilot.agents.investigation.investigate", lambda a, s: fakes.evidence(a))
    monkeypatch.setattr("aml_copilot.agents.investigation.match_typology", lambda e, r, s: fakes.typology(e, r))
    monkeypatch.setattr("aml_copilot.agents.investigation.write_narrative", lambda e, r, a, s: fakes.narrative_step(e, r, a))

    def explode(*args):
        raise AssertionError("the faithfulness check must not run")

    monkeypatch.setattr("aml_copilot.agents.investigation.run_faithfulness", explode)
    state = run_investigation(ACCOUNT, Settings(_env_file=None), check_faithfulness=False)
    assert state["status"] == "draft_ready" and "faithfulness" not in state["steps_completed"]


def test_the_observer_hears_how_long_each_real_step_took(monkeypatch):
    from aml_copilot.config import Settings

    fakes = Fakes()
    monkeypatch.setattr("aml_copilot.agents.investigation.investigate", lambda a, s: fakes.evidence(a))
    monkeypatch.setattr("aml_copilot.agents.investigation.match_typology", lambda e, r, s: fakes.typology(e, r))
    monkeypatch.setattr("aml_copilot.agents.investigation.write_narrative", lambda e, r, a, s: fakes.narrative_step(e, r, a))
    monkeypatch.setattr("aml_copilot.agents.investigation.run_faithfulness", lambda e, r, a, n, s: {"report": report()})
    heard = {}
    run_investigation(ACCOUNT, Settings(_env_file=None), observer=heard.__setitem__)
    assert list(heard) == ["evidence", "typology", "narrative", "faithfulness"]
    assert all(isinstance(seconds, float) and seconds >= 0 for seconds in heard.values())


# ----------------------------------------------------------- the faithfulness step
def report(passed=True, flagged=0, issues=()):
    checks = [StatementCheck(number=n, section="body", text=f"Sentence {n}.", verdict="supported", evidence_quote="q")
              for n in range(1, 5 - flagged)]
    checks += [StatementCheck(number=n, section="body", text=f"Sentence {n}.", verdict="unsupported", reason="no basis")
               for n in range(5 - flagged, 5)]
    return FaithfulnessReport(
        account_id=ACCOUNT, checks=checks, supported=4 - flagged, unsupported=flagged,
        consistency_issues=list(issues), passed=passed and not flagged and not issues,
    )


class FaithfulFakes(Fakes):
    def __init__(self, *, faith=None, faith_error=None, **kw):
        super().__init__(**kw)
        self.faith, self.faith_error = faith or report(), faith_error

    def faithfulness_step(self, evidence, risk_score, asm, narr):
        self.calls.append(("faithfulness", evidence, risk_score, asm, narr))
        return {"error": self.faith_error} if self.faith_error else {"report": self.faith}

    def run(self):
        graph = build_investigation_graph(self.evidence, self.typology, self.narrative_step, self.faithfulness_step)
        return graph.invoke({"account_id": ACCOUNT})


def test_the_faithfulness_step_runs_last_and_receives_the_narrative():
    fakes = FaithfulFakes()
    state = fakes.run()
    assert [c[0] for c in fakes.calls] == ["evidence", "typology", "narrative", "faithfulness"]
    assert fakes.calls[3][4] is fakes.narrative and fakes.calls[3][3] is fakes.asm
    assert state["steps_completed"] == ["evidence", "typology", "narrative", "faithfulness"]
    assert state["faithfulness"].passed and "ready for human review" in state["status_detail"]
    assert "flagged" not in state["status_detail"]  # a passing check adds nothing to the sentence


def test_a_failed_faithfulness_check_still_delivers_the_draft_but_says_what_was_flagged():
    state = FaithfulFakes(faith=report(flagged=2)).run()
    assert state["status"] == "draft_ready" and state["narrative"].status == "draft_for_human_review"
    assert "The faithfulness check flagged 2 item(s) for attention." in state["status_detail"]
    assert [w for w in state["warnings"] if w.startswith("[faithfulness]")] == [
        "[faithfulness] sentence 3 is unsupported: 'Sentence 3.' - no basis",
        "[faithfulness] sentence 4 is unsupported: 'Sentence 4.' - no basis",
    ]


def test_consistency_issues_count_as_flagged_items_and_are_tagged():
    state = FaithfulFakes(faith=report(issues=["wrong score"])).run()
    assert "flagged 1 item(s)" in state["status_detail"] and "[consistency] wrong score" in state["warnings"]


def test_a_judge_that_cannot_run_is_not_fatal_and_is_reported():
    state = FaithfulFakes(faith_error="did not fit the schema").run()
    assert state["status"] == "draft_ready" and "faithfulness" not in state
    assert "could not run, so the draft has not been fact-checked" in state["status_detail"]
    assert "[faithfulness] the check could not run: did not fit the schema" in state["warnings"]
    assert "Faithfulness check: COULD NOT RUN" in render_run(state)


def test_nothing_is_fact_checked_when_nothing_was_drafted():
    fakes = FaithfulFakes(risk_score=risk("low"), asm=assessment(matched=False), narrative=not_drafted())
    state = fakes.run()
    assert "faithfulness" not in [c[0] for c in fakes.calls] and state["status"] == "no_action"
    assert "Faithfulness check: not run" in render_run(state)


def test_a_failing_narrative_step_skips_the_faithfulness_step():
    fakes = FaithfulFakes(fail="narrative")
    state = fakes.run()
    assert "faithfulness" not in [c[0] for c in fakes.calls] and state["status"] == "failed"


def test_the_report_shows_the_verdict_and_lists_the_flagged_sentences_once():
    passed = render_run(FaithfulFakes().run())
    assert "Faithfulness check: PASSED (4 of 4 sentences supported by the evidence)" in passed
    flagged = render_run(FaithfulFakes(faith=report(flagged=1, issues=["wrong score"])).run())
    assert "Faithfulness check: NEEDS ATTENTION (2 item(s) flagged; 3 of 4 supported)" in flagged
    assert "4. UNSUPPORTED: Sentence 4." in flagged and "Consistency: wrong score" in flagged
    assert flagged.count("no basis") == 1  # shown in its own section, not repeated under "Other warnings"
