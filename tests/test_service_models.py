"""The API/storage form of an investigation: JSON-safe, hash-stamped, and free of raw database rows."""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aml_copilot.agents.models import (
    EvidenceSummary,
    NarrativeStatement,
    SarNarrative,
    TypologyAssessment,
)
from aml_copilot.detection import LayeringSignal, RiskScore, StructuringSignal
from aml_copilot.ingestion import EvidenceBundle
from aml_copilot.service.models import (
    DecisionRequest,
    InvestigationResult,
    Provenance,
    StartRequest,
    compute_result_hash,
    to_api_result,
    to_jsonable,
)

SENTINEL = "RAW-ROW-SENTINEL-do-not-leak"
ACCOUNT = "ACC-H-00000"


def narrative(text="A fact."):
    return SarNarrative(
        account_id=ACCOUNT, status="draft_for_human_review",
        introduction=[NarrativeStatement(text="Referred.")],
        facts=[NarrativeStatement(text=text, evidence_ids=["TXN1"])],
        conclusion=[NarrativeStatement(text="The decision rests with the investigator.")],
    )


def run_state(**overrides):
    """A finished run, shaped like the orchestrator's RunState - including the raw `bundle`."""
    state = {
        "account_id": ACCOUNT,
        "status": "draft_ready",
        "status_detail": "A narrative draft is ready for human review.",
        "steps_completed": ["evidence", "typology", "narrative", "faithfulness"],
        "missing_sources": [],
        "warnings": ["[narrative] something"],
        "bundle": EvidenceBundle(account_id=ACCOUNT, transactions=[{"transaction_id": "TXN1", "memo": SENTINEL}]),
        "evidence": EvidenceSummary(
            account_id=ACCOUNT, profile_summary="p", transaction_summary="t", network_summary="n",
            case_history_summary="c", detection_summary="d", citations={"a claim": ["TXN1"]},
        ),
        "risk_score": RiskScore(
            score=100, tier="high", reasons=["both"],
            structuring=StructuringSignal(flagged=True, matched_transaction_ids=["TXN1"], total_amount=27911.45),
            layering=LayeringSignal(flagged=True, chain_transaction_ids=["TXN2"], elapsed=timedelta(hours=13)),
        ),
        "assessment": TypologyAssessment(account_id=ACCOUNT),
        "narrative": narrative(),
    }
    return {**state, **overrides}


# ------------------------------------------------------------------ raw rows never get through
def test_the_raw_database_rows_never_appear_in_the_stored_or_returned_form():
    result = to_api_result(run_state())
    assert "bundle" not in InvestigationResult.model_fields
    assert SENTINEL not in result.model_dump_json()
    assert SENTINEL not in json.dumps(result.model_dump(mode="json"))


def test_an_unknown_key_in_the_run_state_is_ignored_not_passed_along():
    result = to_api_result(run_state(secret_extra={"password": SENTINEL}))
    assert SENTINEL not in result.model_dump_json()


# ------------------------------------------------------------------------- JSON-safe forms
def test_the_risk_score_becomes_plain_json_with_the_timedelta_in_seconds():
    risk = to_api_result(run_state()).risk_score
    assert risk["score"] == 100 and risk["tier"] == "high"
    assert risk["layering"]["elapsed"] == 13 * 3600.0
    assert risk["structuring"]["matched_transaction_ids"] == ["TXN1"]
    json.dumps(risk)  # does not raise


def test_a_signal_with_no_elapsed_time_is_just_null():
    state = run_state(risk_score=RiskScore(score=0, tier="low", layering=LayeringSignal(flagged=False)))
    assert to_api_result(state).risk_score["layering"]["elapsed"] is None


def test_the_rendered_narrative_text_is_included_for_people():
    assert to_api_result(run_state()).narrative_text.startswith(f"SAR NARRATIVE DRAFT - {ACCOUNT}")


def test_a_failed_run_with_most_keys_missing_still_converts():
    state = {"account_id": ACCOUNT, "status": "failed", "status_detail": "The evidence step failed: boom",
             "failed_step": "evidence", "error": "boom", "missing_sources": [], "warnings": []}
    result = to_api_result(state)
    assert result.status == "failed" and result.error == "boom"
    assert result.narrative is None and result.risk_score is None and result.narrative_text is None


@pytest.mark.parametrize(("value", "expected"), [
    (timedelta(minutes=2), 120.0),
    (date(2026, 7, 2), "2026-07-02"),
    (Decimal("9420.50"), 9420.5),
    ({"a": (1, 2), "b": {3}}, {"a": [1, 2], "b": [3]}),
    ("plain", "plain"),
])
def test_to_jsonable_handles_each_awkward_type(value, expected):
    assert to_jsonable(value) == expected


# ----------------------------------------------------------------------------- the hash
def test_the_stamped_hash_matches_a_fresh_computation_and_is_sha256_hex():
    result = to_api_result(run_state())
    assert result.result_hash == compute_result_hash(result)
    assert len(result.result_hash) == 64 and int(result.result_hash, 16) >= 0


def test_the_same_run_gives_the_same_hash_and_a_changed_sentence_changes_it():
    assert to_api_result(run_state()).result_hash == to_api_result(run_state()).result_hash
    assert to_api_result(run_state()).result_hash != to_api_result(run_state(narrative=narrative("A different fact."))).result_hash


def test_editing_the_stored_draft_no_longer_matches_its_hash():
    result = to_api_result(run_state())
    tampered = result.model_copy(update={"status_detail": "edited afterwards"})
    assert compute_result_hash(tampered) != tampered.result_hash


def test_provenance_is_part_of_what_is_hashed():
    plain = to_api_result(run_state())
    stamped = to_api_result(run_state(), Provenance(llm_provider="vertex", models={"narrative": "claude-sonnet-5"}))
    assert plain.result_hash != stamped.result_hash and stamped.provenance.llm_provider == "vertex"


# ------------------------------------------------------------------------ what clients send
@pytest.mark.parametrize("raw", ["ACC-H-00000", " acc-s-00001 ", "ACC-N-99999\t"])
def test_valid_account_ids_are_accepted_and_normalised(raw):
    assert StartRequest(account_id=raw).account_id == raw.strip().upper()


@pytest.mark.parametrize("raw", ["ACC-X-00001", "ACC-S-0001", "", "ACC-S-00001' OR '1'='1", "ACC-S-00001\nACC-S-00002"])
def test_invalid_or_hostile_account_ids_are_rejected_before_anything_else_happens(raw):
    with pytest.raises(ValidationError):
        StartRequest(account_id=raw)


def good_decision(**overrides):
    return {"decision": "approve", "comment": "Reviewed the cited transactions.", "result_hash": "a" * 64, **overrides}


def test_a_well_formed_decision_is_accepted_and_its_comment_trimmed():
    assert DecisionRequest(**good_decision(comment="  ok to proceed  ")).comment == "ok to proceed"


@pytest.mark.parametrize("bad", [
    {"decision": "file_sar"},  # not one of approve / reject / escalate
    {"comment": ""},
    {"comment": "   "},  # blank after trimming
    {"comment": "x" * 2001},
    {"result_hash": "short"},
])
def test_a_malformed_decision_is_rejected(bad):
    with pytest.raises(ValidationError):
        DecisionRequest(**good_decision(**bad))
