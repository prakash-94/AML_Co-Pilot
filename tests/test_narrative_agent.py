"""Narrative Agent: the gate, the prompt, the checks on the model's answer, and the graph."""

import pytest
from pydantic import ValidationError

from aml_copilot.agents.models import (
    EvidenceSummary,
    NarrativeStatement,
    NarrativeText,
    RankedTypologyMatch,
    SarNarrative,
    TypologyAssessment,
)
from aml_copilot.agents.narrative import (
    SYSTEM_PROMPT,
    build_narrative_graph,
    build_narrative_prompt,
    finalize_narrative,
    should_draft,
)
from aml_copilot.detection import LayeringSignal, RiskScore
from aml_copilot.errors import ModelOutputError, SourceUnavailableError

ACCOUNT = "ACC-L-00000"
HOP1, HOP2, HOP3 = "TXN095042", "TXN095043", "TXN095044"
QUESTIONS = ["Who is the first known source of the funds?", "Is the chain explained by documented fees?"]


def evidence(**overrides):
    base = EvidenceSummary(
        account_id=ACCOUNT,
        profile_summary="Emma Wang is listed as a Teacher with a High risk rating. KYC was last reviewed on 2025-11-02.",
        transaction_summary="On 2026-08-01 at 21:36:34, a wire transfer of $44,587.98 was sent from ACC-L-00000 to ACC-LC-00000-0.",
        network_summary=(
            "On 2026-08-02 at 01:36:34, $40,010.80 moved from ACC-LC-00000-0 to ACC-LC-00000-1; "
            "at 14:36:34, $35,894.25 moved on to ACC-LC-00000-2. The chain spans 17 hours."
        ),
        case_history_summary="The account has 9 prior cases.",
        detection_summary="The rule-based detector assigned a score of 50/100 (medium).",
        data_completeness=[],
        citations={
            "wire of $44,587.98": [HOP1],
            "wire of $40,010.80": [HOP2],
            "wire of $35,894.25": [HOP3],
            "prior SAR filed": ["CASE-L-010361"],
        },
    )
    return base.model_copy(update=overrides)


def risk(tier="medium", flagged=True):
    return RiskScore(
        score=50 if tier == "medium" else 0,
        tier=tier,
        reasons=["Layering signal: 3-hop chain completed in 17:00:00 with shrinking amounts"] if flagged else [],
        layering=LayeringSignal(flagged=flagged, chain_transaction_ids=[HOP1, HOP2, HOP3] if flagged else []),
    )


def match(**overrides):
    base = RankedTypologyMatch(
        typology="Rapid wire transfers (layering)",
        source_document="rapid_wire_transfers.md",
        confidence="high",
        matched_red_flags=["A chain of transfers completed in a short span"],
        unassessable_red_flags=["Deposit and withdrawal locations"],
        rationale="A 3-hop chain completed in 17 hours with shrinking amounts.",
        evidence_ids=[HOP1, HOP2, HOP3],
        detector_corroborated=True,
    )
    return base.model_copy(update=overrides)


def assessment(matches=None):
    return TypologyAssessment(account_id=ACCOUNT, matches=[match()] if matches is None else matches)


def statement(text, *ids):
    return NarrativeStatement(text=text, evidence_ids=list(ids))


def good_text():
    return NarrativeText(
        introduction=[
            statement("This account is referred for review after the rule engine flagged a transfer chain.", HOP1, HOP2, HOP3)
        ],
        facts=[
            statement("The account holder is a Teacher with a High risk rating."),
            statement("On 2026-08-01 a wire transfer of $44,587.98 was sent from ACC-L-00000 to ACC-LC-00000-0.", HOP1),
            statement("On 2026-08-02 $40,010.80 moved from ACC-LC-00000-0 to ACC-LC-00000-1.", HOP2),
        ],
        conclusion=[statement("Deposit and withdrawal locations could not be assessed. The decision rests with the investigator.")],
    )


def finalize(text, ev=None, rk=None, asm=None, questions=None):
    return finalize_narrative(ev or evidence(), rk or risk(), asm or assessment(), text, questions or [])


# ------------------------------------------------------------------------ the gate
def test_a_validated_match_is_worth_drafting_even_at_a_low_score():
    assert should_draft(risk("low", flagged=False), assessment())


def test_a_medium_or_high_score_is_worth_drafting_without_a_match():
    assert should_draft(risk("medium"), assessment([]))
    assert should_draft(risk("high"), assessment([]))


def test_a_clean_account_with_no_match_is_not_worth_drafting():
    assert not should_draft(risk("low", flagged=False), assessment([]))


def test_a_clean_account_never_reaches_the_model():
    def explode(prompt):
        raise AssertionError("the model must not be called for a clean account")

    graph = build_narrative_graph(questions_fn=lambda sources: QUESTIONS, write_fn=explode)
    state = graph.invoke({"evidence": evidence(), "risk_score": risk("low", flagged=False), "assessment": assessment([])})
    narrative = state["narrative"]
    assert narrative.status == "not_drafted" and "nothing to report" in narrative.skip_reason
    assert narrative.facts == [] and narrative.open_questions == []
    assert "No narrative drafted" in narrative.render()


# ---------------------------------------------------------------------- the prompt
def test_the_prompt_carries_the_facts_the_matches_and_the_citable_ids():
    prompt = build_narrative_prompt(evidence(), risk(), assessment())
    assert SYSTEM_PROMPT in prompt
    assert "$44,587.98" in prompt and "Teacher" in prompt
    assert "Rapid wire transfers (layering)" in prompt and "the rule engine independently flagged this pattern" in prompt
    assert "red flag supported: A chain of transfers completed in a short span" in prompt
    assert "red flag NOT assessable from the data: Deposit and withdrawal locations" in prompt
    assert f"also citable): {HOP1}, {HOP2}, {HOP3}" in prompt
    assert "wire of $40,010.80: TXN095043" in prompt


def test_the_prompt_says_so_when_nothing_matched_or_no_detector_exists():
    assert "Matched typologies: none" in build_narrative_prompt(evidence(), risk(), assessment([]))
    no_detector = assessment([match(detector_corroborated=None)])
    assert "no rule-based detector exists" in build_narrative_prompt(evidence(), risk(), no_detector)
    not_flagged = assessment([match(detector_corroborated=False)])
    assert "did NOT flag" in build_narrative_prompt(evidence(), risk(), not_flagged)


def test_the_prompt_lists_data_gaps():
    prompt = build_narrative_prompt(evidence(data_completeness=["No Salesforce KYC profile found"]), risk(), assessment())
    assert "Data completeness notes: No Salesforce KYC profile found" in prompt


# ------------------------------------------------------------ checking the answer
def test_a_clean_answer_passes_through_with_code_set_fields():
    narrative = finalize(good_text(), questions=QUESTIONS)
    assert narrative.status == "draft_for_human_review"
    assert narrative.typologies == ["Rapid wire transfers (layering)"]
    assert narrative.open_questions == QUESTIONS  # word for word, from code
    assert [s.text for s in narrative.facts][1].startswith("On 2026-08-01")
    assert narrative.facts[1].evidence_ids == [HOP1]
    assert narrative.warnings == []


def test_a_sentence_with_an_amount_not_in_the_evidence_is_dropped():
    text = good_text()
    text.facts.append(statement("A further transfer of $99,999.00 followed.", HOP3))
    narrative = finalize(text)
    assert all("99,999" not in s.text for s in narrative.facts)
    assert any("amount $99,999.00 not found" in w for w in narrative.warnings)


def test_a_sentence_with_a_date_not_in_the_evidence_is_dropped():
    text = good_text()
    text.facts.append(statement("On 2026-09-15 the account was closed."))
    narrative = finalize(text)
    assert all("2026-09-15" not in s.text for s in narrative.facts)
    assert any("date 2026-09-15 not found" in w for w in narrative.warnings)


def test_a_sentence_naming_an_id_not_in_the_evidence_is_dropped():
    text = good_text()
    text.facts.append(statement("Transaction TXN999999 was also involved.", HOP3))
    text.facts.append(statement("Funds reached ACC-LC-00000-9 afterwards."))
    narrative = finalize(text)
    assert len(narrative.facts) == 3
    assert any("id TXN999999" in w for w in narrative.warnings)
    assert any("id ACC-LC-00000-9" in w for w in narrative.warnings)


def test_amounts_are_compared_by_value_not_by_formatting():
    text = good_text()
    text.facts.append(statement("The first hop was $44587.98 in total.", HOP1))
    narrative = finalize(text)
    assert any("$44587.98" in s.text for s in narrative.facts) and narrative.warnings == []


def test_ids_the_model_invented_are_stripped_but_the_sentence_stays():
    text = good_text()
    text.facts[1] = statement("On 2026-08-01 a wire transfer of $44,587.98 left ACC-L-00000.", HOP1, "TXN000000")
    narrative = finalize(text)
    assert narrative.facts[1].evidence_ids == [HOP1]
    assert any("Removed unknown evidence ids" in w and "TXN000000" in w for w in narrative.warnings)


def test_comma_joined_real_ids_are_split_and_rule_engine_ids_are_always_citable():
    text = good_text()
    text.facts[1] = statement("On 2026-08-01 a wire transfer of $44,587.98 left ACC-L-00000.", f"{HOP1}, {HOP2}")
    only_rule_engine = evidence(citations={"prior SAR filed": ["CASE-L-010361"]})
    narrative = finalize(text, ev=only_rule_engine)
    assert narrative.facts[1].evidence_ids == [HOP1, HOP2]  # not cited by the Evidence Agent, flagged by the rules
    assert narrative.warnings == []


def test_an_id_named_in_the_sentence_counts_as_cited_even_if_the_field_is_empty():
    text = good_text()
    text.facts[1] = statement(f"On 2026-08-01 a wire transfer of $44,587.98 was sent ({HOP1}).")
    narrative = finalize(text)
    assert narrative.facts[1].evidence_ids == [HOP1]
    assert not any("cites no evidence id" in w for w in narrative.warnings)


def test_naming_an_account_id_does_not_count_as_citing_it():
    text = good_text()
    text.facts[1] = statement("On 2026-08-01 a wire transfer of $44,587.98 left ACC-L-00000.")
    narrative = finalize(text, ev=evidence(citations={**evidence().citations, "kyc": [ACCOUNT]}))
    assert narrative.facts[1].evidence_ids == []  # ACC- ids are not transaction/case citations
    assert any("gives an amount but cites no evidence id" in w for w in narrative.warnings)


def test_the_prompt_asks_for_prior_cases_briefly():
    assert "Mention prior cases briefly" in SYSTEM_PROMPT


def test_an_amount_with_no_evidence_id_is_kept_but_warned_about():
    text = good_text()
    text.facts[1] = statement("On 2026-08-01 a wire transfer of $44,587.98 was sent.")
    narrative = finalize(text)
    assert len(narrative.facts) == 3
    assert any("gives an amount but cites no evidence id" in w for w in narrative.warnings)


def test_verdict_like_wording_is_kept_but_warned_about():
    text = good_text()
    text.conclusion.append(statement("The activity is suspicious."))
    narrative = finalize(text)
    assert any(s.text == "The activity is suspicious." for s in narrative.conclusion)
    assert any("'suspicious'" in w and "conclusion" in w for w in narrative.warnings)


def test_blank_sentences_are_skipped_and_an_emptied_section_is_reported():
    text = NarrativeText(
        introduction=[statement("   ")],
        facts=[statement("A transfer of $1.00 occurred.", HOP1)],
        conclusion=[statement("Done.")],
    )
    narrative = finalize(text)
    assert narrative.introduction == [] and narrative.facts == []
    assert any("introduction is empty" in w for w in narrative.warnings)
    assert any("body is empty" in w for w in narrative.warnings)
    assert "nothing - every statement failed validation" in narrative.render()


def test_a_flagged_transaction_that_no_statement_cites_is_reported():
    text = good_text()
    text.introduction = [statement("This account is referred for review.")]  # no longer cites the chain
    text.facts[2] = statement("On 2026-08-02 $40,010.80 moved from ACC-LC-00000-0 to ACC-LC-00000-1.")
    narrative = finalize(text)
    assert any(
        "no statement cites" in w and HOP2 in w and HOP3 in w and HOP1 not in w for w in narrative.warnings
    )


def test_the_prompt_asks_for_who_first():
    assert "Begin with who the account holder is" in SYSTEM_PROMPT


def test_the_model_cannot_set_the_status_or_the_questions():
    assert "status" not in NarrativeText.model_fields and "open_questions" not in NarrativeText.model_fields


def test_the_models_answer_must_have_all_three_parts():
    with pytest.raises(ValidationError):
        NarrativeText(introduction=[], facts=[statement("x")], conclusion=[statement("y")])


# ---------------------------------------------------------------------- rendering
def test_render_shows_each_sentence_with_its_ids_the_questions_and_the_warnings():
    text = good_text()
    text.facts.append(statement("A further transfer of $99,999.00 followed.", HOP3))
    rendered = finalize(text, questions=QUESTIONS).render()
    assert rendered.startswith(f"SAR NARRATIVE DRAFT - {ACCOUNT} (pending human review)")
    assert f"$44,587.98 was sent from ACC-L-00000 to ACC-LC-00000-0. [{HOP1}]" in rendered
    assert "Questions for the investigator to verify" in rendered and f"  - {QUESTIONS[0]}" in rendered
    assert "Warnings" in rendered and "99,999" in rendered  # the dropped sentence is reported, not shown as text


# -------------------------------------------------------------------------- the graph
def test_the_graph_drafts_a_checked_narrative_with_questions_for_the_matched_documents():
    asked = []

    def questions_fn(sources):
        asked.append(sources)
        return QUESTIONS

    graph = build_narrative_graph(questions_fn=questions_fn, write_fn=lambda prompt: good_text())
    state = graph.invoke({"evidence": evidence(), "risk_score": risk(), "assessment": assessment()})
    assert asked == [["rapid_wire_transfers.md"]]
    assert state["narrative"].status == "draft_for_human_review"
    assert state["narrative"].open_questions == QUESTIONS


def test_a_schema_mismatch_from_the_model_is_recorded_in_state():
    def bad_writer(prompt):
        raise ModelOutputError("did not fit the schema")

    graph = build_narrative_graph(questions_fn=lambda sources: QUESTIONS, write_fn=bad_writer)
    state = graph.invoke({"evidence": evidence(), "risk_score": risk(), "assessment": assessment()})
    assert "did not fit the schema" in state["error"] and "narrative" not in state


def test_a_failure_loading_questions_is_recorded_and_the_model_is_never_called():
    def broken_questions(sources):
        raise SourceUnavailableError("typology documents", "folder missing")

    def explode(prompt):
        raise AssertionError("the model must not be called")

    graph = build_narrative_graph(questions_fn=broken_questions, write_fn=explode)
    state = graph.invoke({"evidence": evidence(), "risk_score": risk(), "assessment": assessment()})
    assert "folder missing" in state["error"]


# --------------------------------------------------------------- which model writes it
def test_the_writer_uses_narrative_model_when_set_else_the_default(monkeypatch):
    from pydantic import SecretStr

    from aml_copilot.agents.narrative import make_anthropic_writer
    from aml_copilot.config import Settings

    asked = []

    class FakeChat:
        def with_structured_output(self, schema):
            return None

    def fake_make_chat_model(settings, max_tokens=None, model=None):
        asked.append(model)
        return FakeChat()

    monkeypatch.setattr("aml_copilot.agents.narrative.make_chat_model", fake_make_chat_model)
    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("sk-test"), anthropic_model="claude-haiku-4-5")
    make_anthropic_writer(settings)
    make_anthropic_writer(settings.model_copy(update={"narrative_model": "claude-sonnet-5"}))
    assert asked == [None, "claude-sonnet-5"]


def test_sar_narrative_not_drafted_needs_no_text():
    narrative = SarNarrative(account_id=ACCOUNT, status="not_drafted", skip_reason="nothing")
    assert narrative.facts == [] and narrative.warnings == []
