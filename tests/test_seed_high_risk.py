"""The high-risk demo accounts: the plan is well-formed AND the real detectors score it 'high'."""

from datetime import datetime, timedelta

import pytest

import seed_high_risk_accounts as seed
from aml_copilot.detection import run_detection
from aml_copilot.ingestion import EvidenceBundle
from aml_copilot.ingestion.models import RelatedAccount

PLAN = seed.build_plan()
EXISTING_CASE_TYPES = {
    "Dormant Account Activity", "High-Risk Country Transfer", "Large Cash Transaction", "Rapid Fund Movement",
    "Round-Dollar Transaction Pattern", "Structuring Alert", "Unusual Wire Pattern", "Velocity Alert",
}
EXISTING_DISPOSITIONS = {
    "Closed - Insufficient Evidence", "Escalated - Under Review", "No Action - False Positive", "SAR Filed", None,
}


def rows():
    return [dict(zip(seed.TXN_COLUMNS, row, strict=True)) for row in PLAN["transactions"]]


def bundle_for(account_number: int) -> EvidenceBundle:
    """What ingestion + network expansion would hand the detectors for one account."""
    acc = f"ACC-H-{account_number:05d}"
    own = [r for r in rows() if r["account_id"] == acc]
    chain = [r for r in rows() if r["account_id"].startswith(f"ACC-HC-{account_number:05d}-")]
    related = [{**r, "hop": hop} for hop, r in enumerate(sorted(chain, key=lambda r: r["timestamp"]), start=2)]
    return EvidenceBundle(
        account_id=acc,
        transactions=own,
        related_transactions=related,
        related_accounts=[RelatedAccount(account_id=f"ACC-HC-{account_number:05d}-{i}", hop=i + 1) for i in range(3)],
        network_expanded=True,
    )


# ----------------------------------------------------------------- the plan itself
def test_there_are_three_accounts_with_seven_transactions_each():
    assert len(PLAN["transactions"]) == 21 and len(PLAN["profiles"]) == 3 and len(PLAN["cases"]) == 9


def test_transaction_ids_are_unique_and_continue_from_the_current_end_of_the_table():
    ids = [row[0] for row in PLAN["transactions"]]
    assert ids == [f"TXN{n:06d}" for n in range(99_482, 99_482 + 21)]


def test_account_ids_pass_the_projects_own_validation():
    from aml_copilot.ids import validate_account_id

    assert [validate_account_id(p["Account_Id__c"]) for p in PLAN["profiles"]] == [
        "ACC-H-00000", "ACC-H-00001", "ACC-H-00002",
    ]


def test_every_deposit_is_near_but_under_the_ten_thousand_dollar_threshold_within_90_days():
    for number in range(3):
        deposits = [r for r in rows() if r["account_id"] == f"ACC-H-{number:05d}" and r["type"] == "cash_deposit"]
        assert len(deposits) == 3 and all(7_000 <= d["amount"] < 10_000 for d in deposits)
        assert len({d["branch_id"] for d in deposits}) == 3  # spread over three branches
        times = sorted(datetime.fromisoformat(d["timestamp"]) for d in deposits)
        assert times[-1] - times[0] <= timedelta(days=90)


def test_the_wire_chain_is_linked_shrinking_fast_and_crosses_borders():
    for number in range(3):
        wires = sorted(
            (r for r in rows() if r["type"] == "wire_transfer" and f"-{number:05d}" in (r["account_id"] + r["counterparty_account"])),
            key=lambda r: r["timestamp"],
        )
        assert len(wires) == 3
        assert wires[0]["account_id"] == f"ACC-H-{number:05d}"
        assert [w["counterparty_account"] for w in wires[:2]] == [w["account_id"] for w in wires[1:]]  # each hop sends on
        amounts = [w["amount"] for w in wires]
        assert amounts == sorted(amounts, reverse=True) and len(set(amounts)) == 3
        times = [datetime.fromisoformat(w["timestamp"]) for w in wires]
        assert times[-1] - times[0] <= timedelta(hours=72)
        assert len({w["country"] for w in wires}) == 3


def test_labels_use_only_the_three_existing_ground_truth_values():
    assert {r["ground_truth_label"] for r in rows()} == {"structuring", "layering", "normal"}


def test_profiles_and_cases_use_the_existing_vocabulary():
    assert all(p["Risk_Rating__c"] == "High" for p in PLAN["profiles"])
    assert [p["Is_PEP__c"] for p in PLAN["profiles"]] == [True, False, False]
    case_ids = [c[0] for c in PLAN["cases"]]
    assert case_ids == [f"CASE-H-{n:06d}" for n in range(1, 10)]
    assert {c[2] for c in PLAN["cases"]} <= EXISTING_CASE_TYPES
    assert {c[6] for c in PLAN["cases"]} <= EXISTING_DISPOSITIONS
    assert all((c[5] == "Open") == (c[6] is None) for c in PLAN["cases"])  # open cases have no disposition


def test_the_plan_is_deterministic():
    assert seed.build_plan() == seed.build_plan()


# ------------------------------------------------ the point: the REAL detectors say HIGH
@pytest.mark.parametrize("number", [0, 1, 2])
def test_the_real_detectors_score_each_account_high(number):
    score = run_detection(bundle_for(number))
    assert score.structuring.flagged and score.layering.flagged
    assert score.score == 100 and score.tier == "high"
    assert score.layering.crosses_border and score.layering.shrinking_amounts
    assert len(score.layering.chain_transaction_ids) == 3


def test_the_existing_demo_accounts_keep_their_tiers_because_nothing_existing_is_touched():
    # medium needs exactly one signal: neither plan row can be mistaken for an ACC-S/L/N account
    assert all(not r["account_id"].startswith(("ACC-S-", "ACC-L-", "ACC-N-", "ACC-LC-")) for r in rows())
