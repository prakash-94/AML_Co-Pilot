"""Detection tests - synthetic fixtures for the rules, no live credentials needed."""

from datetime import timedelta

from aml_copilot.detection import (
    LayeringConfig,
    RiskScoreConfig,
    detect_layering,
    detect_structuring,
    run_detection,
    score_account,
)
from aml_copilot.detection.layering import LayeringSignal
from aml_copilot.detection.structuring import StructuringConfig, StructuringSignal
from aml_copilot.ingestion import EvidenceBundle, RelatedAccount

ACC = "ACC-S-00000"


def cash(tid, amount, timestamp, branch="B1"):
    return {"transaction_id": tid, "account_id": ACC, "type": "cash_deposit", "amount": amount,
            "timestamp": timestamp, "branch_id": branch}


def wire(tid, src, dst, amount, timestamp, hop=None, country="US"):
    row = {"transaction_id": tid, "account_id": src, "counterparty_account": dst,
           "type": "wire_transfer", "amount": amount, "timestamp": timestamp, "country": country}
    if hop is not None:
        row["hop"] = hop
    return row


# --------------------------------------------------------------- structuring
def test_flags_three_near_threshold_deposits_in_window():
    txns = [
        cash("t1", 7700, "2026-08-01T09:00:00", branch="B1"),
        cash("t2", 8100, "2026-08-02T10:00:00", branch="B2"),
        cash("t3", 7900, "2026-08-03T11:00:00", branch="B1"),
    ]
    signal = detect_structuring(txns)
    assert signal.flagged
    assert signal.matched_transaction_ids == ["t1", "t2", "t3"]
    assert signal.total_amount == 7700 + 8100 + 7900
    assert signal.distinct_branches == ["B1", "B2"]
    assert "across 2 branches" in signal.reason


def test_not_flagged_below_minimum_count():
    txns = [cash("t1", 7700, "2026-08-01"), cash("t2", 8100, "2026-08-02")]
    signal = detect_structuring(txns)
    assert not signal.flagged
    assert "Fewer than the minimum" in signal.reason


def test_not_flagged_when_spread_outside_the_window():
    txns = [
        cash("t1", 7700, "2026-08-01"),
        cash("t2", 8100, "2026-08-20"),
        cash("t3", 7900, "2026-09-10"),
    ]
    signal = detect_structuring(txns, StructuringConfig(window_days=7))
    assert not signal.flagged
    assert "not clustered" in signal.reason


def test_ignores_amounts_outside_the_near_threshold_band():
    txns = [
        cash("t1", 500, "2026-08-01"),      # too small
        cash("t2", 9999, "2026-08-01"),     # in band
        cash("t3", 12000, "2026-08-01"),    # at/over threshold, excluded (>= threshold)
    ]
    signal = detect_structuring(txns)
    assert not signal.flagged  # only 1 candidate qualifies


def test_ignores_non_cash_deposit_types():
    txns = [wire("t1", ACC, "ACC-N-00001", 7700, "2026-08-01"),
            wire("t2", ACC, "ACC-N-00002", 8100, "2026-08-01"),
            wire("t3", ACC, "ACC-N-00003", 7900, "2026-08-01")]
    assert not detect_structuring(txns).flagged


def test_picks_the_largest_qualifying_cluster():
    # two deposits far apart, then three close together - the group of 3 should win
    txns = [
        cash("t1", 7100, "2026-08-01"),
        cash("t2", 7200, "2026-08-20"),
        cash("t3", 7300, "2026-09-01"),
        cash("t4", 7400, "2026-09-02"),
        cash("t5", 7500, "2026-09-03"),
    ]
    # Explicit short window here: the point of this test is "largest cluster wins", which
    # needs a window narrow enough to actually exclude t1/t2 - unlike the real 90-day default.
    signal = detect_structuring(txns, StructuringConfig(window_days=5))
    assert signal.flagged
    assert signal.matched_transaction_ids == ["t3", "t4", "t5"]


# ------------------------------------------------------------------ layering
LC0, LC1, LC2 = "ACC-LC-00000-0", "ACC-LC-00000-1", "ACC-LC-00000-2"
ROOT = "ACC-L-00000"


def chain_bundle(**overrides):
    defaults = {
        "account_id": ROOT,
        "transactions": [wire("t1", ROOT, LC0, 44587.98, "2026-08-01T21:36:34")],
        "related_transactions": [
            wire("t2", LC0, LC1, 40010.80, "2026-08-02T01:36:34", hop=2),
            wire("t3", LC1, LC2, 35894.25, "2026-08-02T14:36:34", hop=3),
        ],
        "related_accounts": [
            RelatedAccount(account_id=LC0, hop=1),
            RelatedAccount(account_id=LC1, hop=2),
            RelatedAccount(account_id=LC2, hop=3),
        ],
    }
    return EvidenceBundle(**{**defaults, **overrides})


def test_flags_the_real_layering_chain_shape():
    signal = detect_layering(chain_bundle())
    assert signal.flagged
    assert signal.hop_count == 3
    assert signal.chain_transaction_ids == ["t1", "t2", "t3"]
    assert signal.elapsed == timedelta(hours=17)
    assert signal.shrinking_amounts
    assert not signal.crosses_border


def test_not_flagged_with_no_related_accounts():
    bundle = EvidenceBundle(account_id=ROOT, transactions=[wire("t1", ROOT, "ACC-N-1", 100, "2026-08-01")])
    signal = detect_layering(bundle)
    assert not signal.flagged
    assert signal.hop_count == 0


def test_not_flagged_when_chain_is_too_slow():
    bundle = chain_bundle(related_transactions=[
        wire("t2", LC0, LC1, 40010.80, "2026-08-05T01:36:34", hop=2),
        wire("t3", LC1, LC2, 35894.25, "2026-08-10T14:36:34", hop=3),
    ])
    signal = detect_layering(bundle, LayeringConfig(max_window=timedelta(hours=72)))
    assert not signal.flagged
    assert "spans longer" in signal.reason


def test_crosses_border_when_countries_differ():
    bundle = chain_bundle(transactions=[wire("t1", ROOT, LC0, 44587.98, "2026-08-01T21:36:34", country="US")],
                          related_transactions=[
                              wire("t2", LC0, LC1, 40010.80, "2026-08-02T01:36:34", hop=2, country="MX"),
                              wire("t3", LC1, LC2, 35894.25, "2026-08-02T14:36:34", hop=3, country="MX"),
                          ])
    assert detect_layering(bundle).crosses_border


def test_not_shrinking_when_a_later_hop_is_larger():
    bundle = chain_bundle(related_transactions=[
        wire("t2", LC0, LC1, 40010.80, "2026-08-02T01:36:34", hop=2),
        wire("t3", LC1, LC2, 99999.99, "2026-08-02T14:36:34", hop=3),
    ])
    assert not detect_layering(bundle).shrinking_amounts


# --------------------------------------------------------------- risk score
def test_score_combines_both_signals():
    s = StructuringSignal(flagged=True, reason="x")
    l = LayeringSignal(flagged=True, reason="y")
    result = score_account(s, l)
    assert result.score == 100
    assert result.tier == "high"
    assert len(result.reasons) == 2


def test_score_zero_when_nothing_flagged():
    result = score_account(StructuringSignal(flagged=False), LayeringSignal(flagged=False))
    assert result.score == 0
    assert result.tier == "low"
    assert result.reasons == ["No structuring or layering signals detected"]


def test_score_thresholds_are_configurable():
    s = StructuringSignal(flagged=True, reason="x")
    l = LayeringSignal(flagged=False)
    cfg = RiskScoreConfig(structuring_weight=40, medium_threshold=40, high_threshold=80)
    result = score_account(s, l, cfg)
    assert result.score == 40
    assert result.tier == "medium"


def test_run_detection_end_to_end_on_a_layering_bundle():
    result = run_detection(chain_bundle())
    assert result.layering.flagged
    assert not result.structuring.flagged  # no cash deposits in this fixture
    assert result.score == 50
    assert result.tier == "medium"


def test_run_detection_end_to_end_on_a_clean_bundle():
    bundle = EvidenceBundle(account_id="ACC-N-00000", transactions=[
        wire("t1", "ACC-N-00000", "ACC-N-99999", 500, "2026-08-01"),
    ])
    result = run_detection(bundle)
    assert result.score == 0 and result.tier == "low"


# ------------------------------------------- the reason text carries every established fact
def test_reason_states_the_chain_stays_within_one_country():
    reason = detect_layering(chain_bundle()).reason
    assert reason == "3-hop transfer chain completed in 17:00:00; amounts shrink at each hop; stays within one country (US)"


def test_reason_states_when_the_chain_crosses_a_border():
    bundle = chain_bundle(related_transactions=[
        wire("t2", LC0, LC1, 40010.80, "2026-08-02T01:36:34", hop=2, country="MX"),
        wire("t3", LC1, LC2, 35894.25, "2026-08-02T14:36:34", hop=3, country="MX"),
    ])
    assert "crosses borders (US -> MX)" in detect_layering(bundle).reason


def test_reason_says_nothing_about_borders_when_there_is_no_country_data():
    bundle = chain_bundle(
        transactions=[{k: v for k, v in wire("t1", ROOT, LC0, 44587.98, "2026-08-01T21:36:34").items() if k != "country"}],
        related_transactions=[
            {k: v for k, v in wire("t2", LC0, LC1, 40010.80, "2026-08-02T01:36:34", hop=2).items() if k != "country"},
            {k: v for k, v in wire("t3", LC1, LC2, 35894.25, "2026-08-02T14:36:34", hop=3).items() if k != "country"},
        ],
    )
    reason = detect_layering(bundle).reason
    assert "country" not in reason and "border" not in reason


def test_route_lists_each_country_once_in_the_order_the_money_visited_them():
    # the real chain's shape: US, then two hops in KY
    bundle = chain_bundle(related_transactions=[
        wire("t2", LC0, LC1, 40010.80, "2026-08-02T01:36:34", hop=2, country="KY"),
        wire("t3", LC1, LC2, 35894.25, "2026-08-02T14:36:34", hop=3, country="KY"),
    ])
    signal = detect_layering(bundle)
    assert signal.crosses_border
    assert "crosses borders (US -> KY)" in signal.reason  # KY appears once, not twice
