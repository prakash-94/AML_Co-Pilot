"""Guardrails: the deterministic consistency checks and the faithfulness judge's verification."""

import pytest

from aml_copilot.agents.models import (
    EvidenceSummary,
    NarrativeStatement,
    PassageRef,
    RankedTypologyMatch,
    SarNarrative,
    TypologyAssessment,
)
from aml_copilot.detection import LayeringSignal, RiskScore
from aml_copilot.errors import ModelOutputError
from aml_copilot.guardrails import (
    FaithfulnessConfig,
    FaithfulnessJudgment,
    build_faithfulness_prompt,
    check_consistency,
    check_faithfulness,
    finalize_faithfulness,
    run_faithfulness,
)
from aml_copilot.guardrails.faithfulness import JudgedStatement, quote_in_evidence

ACCOUNT = "ACC-L-00000"
HOPS = ["TXN095042", "TXN095043", "TXN095044"]
TYPOLOGY = "Rapid wire transfers (layering)"


# ---------------------------------------------------------------------- test data
def evidence():
    return EvidenceSummary(
        account_id=ACCOUNT,
        profile_summary="Emma Wang is listed as a Teacher with a High risk rating and is flagged as a PEP.",
        transaction_summary="On 2026-08-01 a wire transfer of $44,587.98 was sent from ACC-L-00000 to ACC-LC-00000-0.",
        network_summary="$40,010.80 moved on to ACC-LC-00000-1 and $35,894.25 to ACC-LC-00000-2. The chain spans 17 hours.",
        case_history_summary="The account has 9 prior cases.",
        detection_summary="The rule-based detector assigned a score of 50/100 (medium).",
        citations={"wire of $44,587.98": HOPS[:1], "prior SAR filed": ["CASE-L-010361"]},
    )


def risk():
    return RiskScore(score=50, tier="medium", reasons=["Layering signal: 3-hop chain in 17 hours"],
                     layering=LayeringSignal(flagged=True, chain_transaction_ids=HOPS))


def assessment():
    return TypologyAssessment(
        account_id=ACCOUNT,
        matches=[RankedTypologyMatch(
            typology=TYPOLOGY, source_document="rapid_wire_transfers.md", confidence="high",
            matched_red_flags=["A chain of transfers completed in a short span"],
            unassessable_red_flags=["Deposit and withdrawal locations"],
            rationale="A 3-hop chain completed in 17 hours.", evidence_ids=HOPS, detector_corroborated=True)],
        passages=[
            PassageRef(source="rapid_wire_transfers.md", heading="Definition", distance=0.4,
                       text="Layering disguises the origin of illicit funds by moving them rapidly."),
            PassageRef(source="structuring.md", heading="Definition", distance=0.5, text="Structuring is breaking a sum."),
        ],
    )


def narrative(*texts, sections=None):
    """A drafted narrative with one sentence per text; by default intro, then body, then conclusion."""
    sections = sections or ["introduction"] + ["facts"] * (len(texts) - 2) + ["conclusion"]
    built = {"introduction": [], "facts": [], "conclusion": []}
    for section, text in zip(sections, texts, strict=True):
        built[section].append(NarrativeStatement(text=text))
    return SarNarrative(account_id=ACCOUNT, status="draft_for_human_review", **built)


GOOD = narrative(
    "The account is referred for review after a layering signal (rule engine score 50/100 (medium)).",
    "Emma Wang is a Teacher with a High risk rating.",
    "The activity is consistent with rapid wire transfers (layering).",
    "Deposit and withdrawal locations could not be assessed.",
)
QUOTES = [
    "assigned a score of 50/100 (medium)",
    "Emma Wang is listed as a Teacher with a High risk rating",
    "A 3-hop chain completed in 17 hours",
    "Deposit and withdrawal locations",
]


def judgment(*rulings):
    return FaithfulnessJudgment(statements=[
        JudgedStatement(number=n, verdict=v, evidence_quote=q, reason=r) for n, (v, q, r) in enumerate(rulings, start=1)
    ])


def all_supported():
    return judgment(*[("supported", quote, "stated") for quote in QUOTES])


def finalize(judged, narr=GOOD, config=None):
    return finalize_faithfulness(evidence(), risk(), assessment(), narr, judged, config)


# ---------------------------------------------------------------------- consistency
def issues_for(*texts):
    narr = narrative(*texts)
    return check_consistency(evidence(), risk(), assessment(), narr)


def test_a_consistent_narrative_has_no_issues():
    assert check_consistency(evidence(), risk(), assessment(), GOOD) == []


def test_a_wrong_score_is_caught():
    found = issues_for("Referred.", "The rule engine scored it 70/100.", "Layering pattern.", "End.")
    assert found == ["The narrative gives a score of 70/100 but the rule engine's score is 50/100"]


def test_a_wrong_tier_is_caught():
    found = issues_for("Referred.", "Score 50/100 (high).", "Layering pattern.", "End.")
    assert any("calls the score 'high' but the rule engine's tier is 'medium'" in issue for issue in found)


def test_a_prior_case_count_missing_from_the_evidence_is_caught():
    found = issues_for("Referred.", "There are 12 prior cases.", "Layering pattern.", "End.")
    assert found == ["The narrative says 12 prior cases but the case history does not give that number"]
    assert issues_for("Referred.", "There are 9 prior cases.", "Layering pattern.", "End.") == []


@pytest.mark.parametrize("mention", [
    "rapid wire transfers (layering)", "Rapid Wire Transfers", "the layering typology",
])
def test_a_matched_typology_may_be_named_in_full_in_part_or_by_its_keyword(mention):
    assert issues_for("Referred.", f"Consistent with {mention}.", "Done.") == []


def test_a_matched_typology_the_narrative_never_names_is_caught():
    found = issues_for("Referred.", "Funds moved quickly.", "Done.")
    assert found == [f"The matched typology '{TYPOLOGY}' is not mentioned in the narrative"]


# ------------------------------------------------------------------- the prompt
def test_the_prompt_has_the_evidence_the_matches_and_the_numbered_sentences():
    prompt = build_faithfulness_prompt(evidence(), risk(), assessment(), GOOD)
    assert "=== ACCOUNT EVIDENCE ===" in prompt and "Emma Wang is listed as a Teacher" in prompt
    assert "Matched typology: Rapid wire transfers (layering) (confidence high)" in prompt
    assert "red flag not assessable from the data: Deposit and withdrawal locations" in prompt
    assert "1. [introduction] The account is referred" in prompt
    assert "2. [body] Emma Wang is a Teacher" in prompt
    assert "4. [conclusion] Deposit and withdrawal locations could not be assessed." in prompt


def test_only_excerpts_of_the_matched_documents_are_shown():
    prompt = build_faithfulness_prompt(evidence(), risk(), assessment(), GOOD)
    assert "Document excerpt (rapid_wire_transfers.md)" in prompt
    assert "structuring.md" not in prompt


# ------------------------------------------------------ verifying the judge's answer
def test_a_fully_supported_draft_with_real_quotes_passes():
    report = finalize(all_supported())
    assert report.passed and report.supported == 4 and report.flagged == []
    assert [c.section for c in report.checks] == ["introduction", "body", "body", "conclusion"]


def test_a_supported_verdict_with_a_quote_that_is_not_in_the_evidence_becomes_unverified():
    rulings = list(all_supported().statements)
    rulings[1] = JudgedStatement(number=2, verdict="supported", evidence_quote="Emma Wang is a notorious criminal", reason="r")
    report = finalize(FaithfulnessJudgment(statements=rulings))
    assert report.checks[1].verdict == "unverified" and "not in the evidence" in report.checks[1].reason
    assert report.unverified == 1 and not report.passed


def test_a_supported_verdict_with_no_quote_at_all_becomes_unverified():
    rulings = list(all_supported().statements)
    rulings[0] = JudgedStatement(number=1, verdict="supported", evidence_quote="", reason="trust me")
    assert finalize(FaithfulnessJudgment(statements=rulings)).checks[0].verdict == "unverified"


def test_a_sentence_the_judge_skipped_is_unverified_never_a_silent_pass():
    report = finalize(FaithfulnessJudgment(statements=list(all_supported().statements)[:3]))
    assert report.checks[3].verdict == "unverified" and "no ruling" in report.checks[3].reason
    assert not report.passed


def test_unsupported_and_contradicted_are_kept_as_ruled_and_fail_the_default_thresholds():
    rulings = list(all_supported().statements)
    rulings[1] = JudgedStatement(number=2, verdict="unsupported", reason="the evidence gives no intent")
    rulings[2] = JudgedStatement(number=3, verdict="contradicted", evidence_quote="A 3-hop chain completed in 17 hours",
                                 reason="it says 17 hours")
    report = finalize(FaithfulnessJudgment(statements=rulings))
    assert (report.unsupported, report.contradicted, report.supported) == (1, 1, 2)
    assert [c.number for c in report.flagged] == [2, 3] and not report.passed


def test_thresholds_decide_how_many_flagged_sentences_a_draft_may_have():
    rulings = list(all_supported().statements)
    rulings[1] = JudgedStatement(number=2, verdict="unsupported", reason="no basis")
    assert not finalize(FaithfulnessJudgment(statements=rulings)).passed
    assert finalize(FaithfulnessJudgment(statements=rulings), config=FaithfulnessConfig(max_unsupported=1)).passed


def test_a_consistency_issue_fails_the_report_even_if_the_judge_approved_everything():
    narr = narrative("Referred.", "The score was 99/100.", "Layering pattern.", "End.")
    judged = judgment(*[("supported", "assigned a score of 50/100 (medium)", "ok")] * 4)
    report = finalize(judged, narr)
    assert report.consistency_issues and not report.passed


def test_if_the_judge_rules_on_a_number_twice_the_first_ruling_counts():
    rulings = [*all_supported().statements, JudgedStatement(number=1, verdict="contradicted", reason="late")]
    assert finalize(FaithfulnessJudgment(statements=rulings)).checks[0].verdict == "supported"


# ------------------------------------------------------------ quote matching rules
@pytest.mark.parametrize(("quote", "expected"), [
    ("Emma Wang is listed as a Teacher", True),
    ("EMMA   WANG is listed as a  teacher", True),  # case and spacing do not matter
    ("“Emma Wang is listed as a Teacher”", True),  # curly quotes are ignored
    ("moved on to ACC-LC-00000-1 ... The chain spans 17 hours", True),  # '...' skips a stretch
    ("moved on to ACC-LC-00000-1 ... a stretch that is not there", False),  # but every piece must exist
    ("Emma Wang is a criminal", False),
    ("", False),
    ("...", False),
    ("a", False),  # too short to prove anything
])
def test_quote_matching(quote, expected):
    from aml_copilot.guardrails.faithfulness import _evidence_block, _normalize

    corpus = _normalize(_evidence_block(evidence(), risk(), assessment()))
    assert quote_in_evidence(quote, corpus) is expected


# ------------------------------------------------------------------ calling the judge
def test_check_faithfulness_sends_the_built_prompt_to_the_judge_and_verifies_the_answer():
    seen = []

    def judge(prompt):
        seen.append(prompt)
        return all_supported()

    report = check_faithfulness(evidence(), risk(), assessment(), GOOD, judge)
    assert seen == [build_faithfulness_prompt(evidence(), risk(), assessment(), GOOD)]
    assert report.passed


def test_a_narrative_with_no_sentences_is_not_sent_to_the_judge():
    empty = SarNarrative(account_id=ACCOUNT, status="draft_for_human_review")

    def explode(prompt):
        raise AssertionError("no model call is needed")

    report = check_faithfulness(evidence(), risk(), assessment(), empty, explode)
    assert report.passed and report.checks == []


def test_run_faithfulness_records_a_model_output_error_instead_of_raising(monkeypatch):
    from pydantic import SecretStr

    from aml_copilot.config import Settings

    def broken_judge(prompt):
        raise ModelOutputError("did not fit the schema")

    monkeypatch.setattr("aml_copilot.guardrails.faithfulness.make_anthropic_faithfulness_judge", lambda s: broken_judge)
    state = run_faithfulness(evidence(), risk(), assessment(), GOOD, Settings(_env_file=None, anthropic_api_key=SecretStr("k")))
    assert state == {"error": "did not fit the schema"}


def test_the_judge_uses_faithfulness_model_when_set_else_the_default(monkeypatch):
    from pydantic import SecretStr

    from aml_copilot.config import Settings
    from aml_copilot.guardrails import make_anthropic_faithfulness_judge

    asked = []

    class FakeChat:
        def with_structured_output(self, schema):
            return None

    def fake_make_chat_model(settings, max_tokens=None, model=None):
        asked.append(model)
        return FakeChat()

    monkeypatch.setattr("aml_copilot.guardrails.faithfulness.make_chat_model", fake_make_chat_model)
    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("k"), anthropic_model="claude-sonnet-5")
    make_anthropic_faithfulness_judge(settings)
    make_anthropic_faithfulness_judge(settings.model_copy(update={"faithfulness_model": "claude-haiku-4-5"}))
    assert asked == [None, "claude-haiku-4-5"]


# ------------------------------------------- what the judge is told (found by the evaluation)
def test_the_judge_sees_which_ids_back_each_evidence_claim():
    prompt = build_faithfulness_prompt(evidence(), risk(), assessment(), GOOD)
    assert "Evidence claim: wire of $44,587.98 (ids: TXN095042)" in prompt
    assert "Evidence claim: prior SAR filed (ids: CASE-L-010361)" in prompt


def test_the_prompt_treats_the_investigator_disclaimer_as_always_supported():
    from aml_copilot.guardrails.faithfulness import SYSTEM_PROMPT

    assert "required disclaimer that makes no claim about the account - it is always supported" in SYSTEM_PROMPT


def test_the_prompt_tells_the_judge_to_check_polarity():
    from aml_copilot.guardrails.faithfulness import SYSTEM_PROMPT

    assert "Check polarity" in SYSTEM_PROMPT and "not flagged as PEP" in SYSTEM_PROMPT


def test_a_judges_answer_sent_as_json_text_is_accepted():
    text = '[{"number": 1, "verdict": "supported", "evidence_quote": "Account: ACC-L-00000", "reason": "ok"}]'
    assert FaithfulnessJudgment.model_validate({"statements": text}).statements[0].verdict == "supported"
