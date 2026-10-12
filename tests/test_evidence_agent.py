"""Evidence Agent tests - fake ingest and fake summarizer, no live credentials or API key."""

import pytest
from pydantic import SecretStr

from aml_copilot.agents.evidence import (
    build_evidence_graph,
    build_evidence_prompt,
    compute_stats,
    investigate,
)
from aml_copilot.agents.models import EvidenceSummary
from aml_copilot.config import Settings
from aml_copilot.detection import RiskScore
from aml_copilot.errors import ConfigError, InvalidAccountIdError
from aml_copilot.ingestion import CaseRecord, CustomerProfile, EvidenceBundle, RelatedAccount

ACC = "ACC-L-00000"


def profile(**kw):
    defaults = {
        "account_id": ACC, "name": "Maria Ivanova", "occupation": "Teacher",
        "risk_rating": "High", "is_pep": True, "kyc_last_reviewed": "2025-11-02",
    }
    return CustomerProfile(**{**defaults, **kw})


def tx(tid, account=ACC, counterparty="ACC-LC-00000-0", amount=44587.98, **extra):
    return {
        "transaction_id": tid, "account_id": account, "counterparty_account": counterparty,
        "amount": amount, "type": "wire_transfer", "timestamp": "2026-08-01T21:36:34", **extra,
    }


def full_bundle(**kw):
    defaults = {
        "account_id": ACC,
        "transactions": [tx("t1")],
        "related_transactions": [tx("t2", account="ACC-LC-00000-0", amount=40010.80, hop=2)],
        "related_accounts": [RelatedAccount(account_id="ACC-LC-00000-0", hop=1)],
        "profile": profile(),
        "case_history": [CaseRecord(case_id=1, account_id=ACC, case_type="layering", status="open")],
    }
    return EvidenceBundle(**{**defaults, **kw})


NO_SIGNAL = RiskScore(score=0, tier="low", reasons=["No structuring or layering signals detected"])


def fake_score_fn(bundle):
    return NO_SIGNAL


class FakeSummarize:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or EvidenceSummary(
            account_id=ACC, profile_summary="x", transaction_summary="x",
            network_summary="x", case_history_summary="x", detection_summary="x",
        )

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self._result


def graph_for(bundle=None, error=None, score=None, summarize=None):
    def ingest_fn(account_id):
        if error:
            raise error
        return bundle

    return build_evidence_graph(ingest_fn, score or fake_score_fn, summarize or FakeSummarize())


# --------------------------------------------------------------- graph wiring
def test_ingest_error_routes_to_end_without_calling_summarize():
    summarize = FakeSummarize()
    result = graph_for(error=InvalidAccountIdError("bad id"), summarize=summarize).invoke({"account_id": "bad"})
    assert result["error"] == "bad id"
    assert "summary" not in result
    assert summarize.calls == []


def test_happy_path_calls_summarize_and_returns_its_result():
    summarize = FakeSummarize()
    bundle = full_bundle()
    result = graph_for(bundle=bundle, summarize=summarize).invoke({"account_id": ACC})
    assert "error" not in result
    assert result["summary"] is summarize._result
    assert len(summarize.calls) == 1
    assert result["bundle"] is bundle


def test_empty_bundle_never_reaches_the_llm():
    summarize = FakeSummarize()
    empty = EvidenceBundle(account_id=ACC, missing_sources=["salesforce"])
    result = graph_for(bundle=empty, summarize=summarize).invoke({"account_id": ACC})
    assert summarize.calls == []
    assert result["summary"].account_id == ACC
    assert "salesforce" in result["summary"].data_completeness[0]
    assert "0/100" in result["summary"].detection_summary


def test_summarizer_account_id_is_corrected_to_the_bundle():
    wrong = EvidenceSummary(
        account_id="ACC-WRONG", profile_summary="x", transaction_summary="x",
        network_summary="x", case_history_summary="x", detection_summary="x",
    )
    result = graph_for(bundle=full_bundle(), summarize=FakeSummarize(wrong)).invoke({"account_id": ACC})
    assert result["summary"].account_id == ACC


def test_score_node_runs_before_summarize_and_its_result_reaches_state():
    calls = []

    def spy_score_fn(bundle):
        calls.append(bundle)
        return RiskScore(score=100, tier="high", reasons=["Structuring signal: x", "Layering signal: y"])

    result = graph_for(bundle=full_bundle(), score=spy_score_fn).invoke({"account_id": ACC})
    assert len(calls) == 1  # scored exactly once, after ingest
    assert result["risk_score"].score == 100 and result["risk_score"].tier == "high"


def test_ingest_error_skips_scoring_too():
    scored = []
    graph_for(error=InvalidAccountIdError("bad id"), score=lambda b: scored.append(b)).invoke({"account_id": "bad"})
    assert scored == []


# --------------------------------------------------------------- prompt content
def test_prompt_includes_profile_transactions_network_and_cases():
    prompt = build_evidence_prompt(full_bundle(), NO_SIGNAL)
    assert ACC in prompt
    assert "Maria Ivanova" in prompt and "is_pep=True" in prompt
    assert "t1" in prompt and "44,587.98" in prompt
    assert "ACC-LC-00000-0 (hop 1)" in prompt
    assert "hop=2" in prompt
    assert "layering" in prompt


def test_prompt_never_leaks_ground_truth():
    prompt = build_evidence_prompt(full_bundle(), NO_SIGNAL)
    assert "ground_truth" not in prompt.lower()


def test_prompt_reports_missing_sources_and_warnings():
    bundle = full_bundle(missing_sources=["salesforce"], warnings=["Transactions truncated to 5 rows"])
    prompt = build_evidence_prompt(bundle, NO_SIGNAL)
    assert "salesforce" in prompt and "truncated to 5 rows" in prompt


def test_prompt_states_absence_plainly_when_no_profile_or_cases():
    bundle = full_bundle(profile=None, case_history=[])
    prompt = build_evidence_prompt(bundle, NO_SIGNAL)
    assert "none found in Salesforce" in prompt
    assert "Case history: none" in prompt


def test_prompt_reports_the_risk_score_as_a_fact_not_a_verdict():
    risk = RiskScore(
        score=100, tier="high",
        reasons=["Structuring signal: 3 cash deposits...", "Layering signal: 3-hop chain..."],
    )
    from aml_copilot.detection import LayeringSignal, StructuringSignal
    risk.structuring = StructuringSignal(flagged=True, matched_transaction_ids=["T1", "T2", "T3"])
    risk.layering = LayeringSignal(flagged=True, chain_transaction_ids=["T1", "T4", "T5"])

    prompt = build_evidence_prompt(full_bundle(), risk)
    assert "100/100 (high)" in prompt
    assert "report as its finding, not your own judgment" in prompt
    assert "['T1', 'T2', 'T3']" in prompt and "['T1', 'T4', 'T5']" in prompt


def test_prompt_reports_a_clean_score_plainly():
    prompt = build_evidence_prompt(full_bundle(), NO_SIGNAL)
    assert "0/100 (low)" in prompt
    assert "No structuring or layering signals detected" in prompt


# --------------------------------------------------------------- compute_stats
def test_compute_stats_empty():
    assert compute_stats([]) == {"count": 0}


def test_compute_stats_aggregates_amounts_types_and_dates():
    rows = [tx("t1", amount=100, type="wire_transfer", timestamp="2026-08-02"),
            tx("t2", amount=50, type="cash_deposit", timestamp="2026-08-01")]
    stats = compute_stats(rows)
    assert stats["count"] == 2
    assert stats["total_amount"] == 150
    assert stats["min_amount"] == 50 and stats["max_amount"] == 100
    assert stats["by_type"] == {"wire_transfer": 1, "cash_deposit": 1}
    assert stats["date_range"] == ("2026-08-01", "2026-08-02")


# --------------------------------------------------------------- investigate()
def test_investigate_requires_anthropic_key_before_touching_ingest():
    settings = Settings(_env_file=None)  # no anthropic_api_key, no source credentials either
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        investigate(ACC, settings=settings)


def test_investigate_accepts_a_configured_key_without_making_a_real_call(monkeypatch):
    # Only proves make_anthropic_summarizer builds without error given a key;
    # no network call is made because we never invoke the returned summarizer.
    from aml_copilot.agents.evidence import make_anthropic_summarizer

    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("sk-test"))
    summarize_fn = make_anthropic_summarizer(settings)
    assert callable(summarize_fn)


# ------------------------------------------------ countries and branches reach the prompt
def test_compute_stats_counts_transactions_per_country():
    rows = [tx("t1", country="US"), tx("t2", country="KY"), tx("t3", country="KY")]
    assert compute_stats(rows)["by_country"] == {"US": 1, "KY": 2}


def test_prompt_names_the_countries_and_branches_of_the_transactions():
    bundle = full_bundle(transactions=[tx("t1", country="KY", branch_id="BR004")])
    prompt = build_evidence_prompt(bundle, NO_SIGNAL)
    assert "countries {'KY': 1}" in prompt
    assert "country=KY branch=BR004" in prompt


def test_prompt_omits_country_text_when_rows_have_none():
    prompt = build_evidence_prompt(full_bundle(), NO_SIGNAL)
    assert "countries" not in prompt and "country=" not in prompt
