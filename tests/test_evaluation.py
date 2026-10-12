"""Evaluation harness: sampling, cost cap, grading, the fault planter, and the loops (fakes only)."""

import re

import pytest

from aml_copilot.agents.models import (
    EvidenceSummary,
    NarrativeStatement,
    RankedTypologyMatch,
    SarNarrative,
    TypologyAssessment,
)
from aml_copilot.config import Settings
from aml_copilot.detection import RiskScore
from aml_copilot.evaluation.__main__ import parse_per_family
from aml_copilot.evaluation.cost import BudgetExceededError, BudgetMeter, usage_cost
from aml_copilot.evaluation.dataset import EXPECTED, make_case, sample_cases
from aml_copilot.evaluation.judge_check import plant_faults, score_judge
from aml_copilot.evaluation.metrics import render_markdown, score_case, summarize
from aml_copilot.evaluation.runner import (
    apply_faithfulness,
    compare_judges,
    drafted_states,
    estimate_cost,
    evaluate,
)
from aml_copilot.guardrails import FaithfulnessJudgment, check_faithfulness
from aml_copilot.guardrails.faithfulness import FaithfulnessReport, JudgedStatement, StatementCheck

ACCOUNT = "ACC-L-00000"


# ============================================================================ dataset
def test_the_sample_is_reproducible_and_has_the_requested_counts():
    first, second = sample_cases(seed=7), sample_cases(seed=7)
    assert first == second
    assert [c.family for c in first].count("N") == 6 and [c.family for c in first].count("H") == 3
    assert sample_cases(seed=7) != sample_cases(seed=8)


def test_every_sampled_account_has_a_valid_id_inside_the_profiled_range():
    from aml_copilot.evaluation.dataset import PROFILED_ACCOUNTS
    from aml_copilot.ids import validate_account_id

    for case in sample_cases():
        assert validate_account_id(case.account_id) == case.account_id
        assert int(case.account_id[-5:]) < PROFILED_ACCOUNTS[case.family]


def test_the_families_are_always_in_the_same_order_whatever_order_they_were_asked_for():
    cases = sample_cases({"H": 1, "N": 1, "L": 1, "S": 1})
    assert [c.family for c in cases] == ["N", "S", "L", "H"]


def test_asking_for_more_accounts_than_exist_is_an_error():
    with pytest.raises(ValueError, match="Only 3 profiled H accounts"):
        sample_cases({"H": 4})


def test_the_expected_answers_follow_the_scoring_rules():
    assert EXPECTED["N"] == ("low", frozenset())
    assert EXPECTED["H"][0] == "high" and EXPECTED["H"][1] == {"structuring.md", "rapid_wire_transfers.md"}
    assert not make_case("N", 0).expects_draft and make_case("S", 0).expects_draft


# ============================================================================== cost
def test_usage_cost_applies_each_models_price_to_its_tokens():
    usage = {
        "claude-haiku-4-5-20251001": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},  # 1 + 5
        "claude-sonnet-5": {"input_tokens": 500_000, "output_tokens": 100_000},  # 1 + 1
    }
    assert usage_cost(usage) == pytest.approx(8.0)
    assert usage_cost({}) == 0.0


def test_an_unknown_model_is_an_error_not_a_silent_zero():
    with pytest.raises(ValueError, match="No price known"):
        usage_cost({"some-other-model": {"input_tokens": 1, "output_tokens": 1}})


def test_the_meter_refuses_a_step_that_would_pass_the_cap_and_allows_one_that_fits():
    meter = BudgetMeter(1.00)
    meter.add(0.90)
    meter.require(0.10)  # exactly fits
    with pytest.raises(BudgetExceededError, match=r"\$0.900 spent of a \$1.00 cap"):
        meter.require(0.11, "evaluating ACC-S-00001")
    assert meter.remaining == pytest.approx(0.10)


# ============================================================================ test data
def evidence():
    return EvidenceSummary(
        account_id=ACCOUNT,
        profile_summary="Emma Wang is a Teacher with a High risk rating and is flagged as a PEP.",
        transaction_summary="On 2026-08-01 a wire transfer of $44,587.98 was sent from ACC-L-00000 to ACC-LC-00000-0.",
        network_summary="n", case_history_summary="The account has 9 prior cases.", detection_summary="d",
        citations={"wire": ["TXN095042"]},
    )


def risk(tier="medium"):
    return RiskScore(score=50, tier=tier, reasons=["Layering signal"])


# Names chosen so the fixture narrative below really does mention them (the consistency check looks).
TYPOLOGY_NAMES = {"rapid_wire_transfers.md": "wire transfer", "structuring.md": "Structuring"}


def assessment(*sources):
    return TypologyAssessment(account_id=ACCOUNT, matches=[
        RankedTypologyMatch(typology=TYPOLOGY_NAMES[src], source_document=src, confidence="high",
                            rationale="r", evidence_ids=["TXN095042"]) for src in sources])


def narrative(*fact_texts, warnings=()):
    return SarNarrative(
        account_id=ACCOUNT, status="draft_for_human_review", warnings=list(warnings),
        introduction=[NarrativeStatement(text="Referred.")],
        facts=[NarrativeStatement(text=t, evidence_ids=["TXN095042"]) for t in fact_texts],
        conclusion=[NarrativeStatement(text="Decision rests with the investigator.")],
    )


WIRE = "On 2026-08-01 a wire transfer of $44,587.98 was sent from ACC-L-00000 to ACC-LC-00000-0."


def good_state(status="draft_ready", tier="medium", sources=("rapid_wire_transfers.md",), faith=None, drafted=True):
    written = narrative("Emma Wang is a Teacher.", WIRE) if drafted else SarNarrative(account_id=ACCOUNT, status="not_drafted")
    state = {"status": status, "risk_score": risk(tier), "assessment": assessment(*sources),
             "evidence": evidence(), "narrative": written}
    if faith is not None:
        state["faithfulness"] = faith
    return state


def report_with(*verdicts, passed=None):
    checks = [StatementCheck(number=i, section="body", text=f"s{i}", verdict=v) for i, v in enumerate(verdicts, 1)]
    counts = {k: sum(1 for v in verdicts if v == k) for k in ("supported", "unsupported", "contradicted", "unverified")}
    return FaithfulnessReport(account_id=ACCOUNT, checks=checks, passed=all(v == "supported" for v in verdicts)
                              if passed is None else passed, **counts)


# ========================================================================== metrics
def test_a_correct_flagged_run_is_graded_correct():
    result = score_case(make_case("L", 0), good_state(), seconds=5, cost=0.05)
    assert result.tier_ok and result.typology_ok and result.draft_ok and result.drafted
    assert result.sentences == 4 and result.faithfulness_passed is None  # the check did not run


def test_a_wrong_tier_a_wrong_typology_and_a_missing_draft_are_each_detected():
    wrong_tier = score_case(make_case("L", 0), good_state(tier="low"))
    wrong_type = score_case(make_case("L", 0), good_state(sources=("structuring.md",)))
    no_draft = score_case(make_case("L", 0), {**good_state(), "narrative": SarNarrative(account_id=ACCOUNT, status="not_drafted")})
    assert not wrong_tier.tier_ok and not wrong_type.typology_ok and not no_draft.draft_ok


def test_a_failed_run_is_graded_as_failed_with_nothing_matched():
    result = score_case(make_case("S", 3), {"status": "failed", "error": "databricks exploded"})
    assert result.status == "failed" and result.tier is None and not result.tier_ok and result.error


def test_faithfulness_numbers_are_read_from_the_attached_report():
    result = score_case(make_case("L", 0), good_state(faith=report_with("supported", "unsupported", "unverified")))
    assert (result.supported, result.unsupported, result.unverified) == (1, 1, 1)
    assert result.faithfulness_passed is False


def test_dropped_sentences_are_counted_from_the_narrative_warnings():
    state = good_state()
    state["narrative"] = narrative("A fact.", warnings=["Dropped a body statement - amount $9 not found", "Other"])
    result = score_case(make_case("L", 0), state)
    assert result.dropped_sentences == 1 and result.narrative_warnings == 2


def test_the_summary_computes_accuracy_precision_recall_and_false_alarms():
    results = [
        score_case(make_case("L", 0), good_state(), cost=0.05),
        score_case(make_case("L", 1), good_state(tier="low", sources=(), drafted=False), cost=0.05),  # a miss
        score_case(make_case("N", 0), good_state(tier="low", sources=(), drafted=False), cost=0.02),
        score_case(make_case("N", 1), good_state(tier="medium", sources=("structuring.md",)), cost=0.02),  # false alarm
        score_case(make_case("H", 0), {"status": "failed", "error": "x"}),
    ]
    summary = summarize(results)
    assert (summary["cases"], summary["failed"]) == (5, 1)
    assert summary["tier_accuracy"] == pytest.approx(2 / 4)  # failed run excluded from the rates
    assert summary["typology_precision"] == pytest.approx(1 / 2) and summary["typology_recall"] == pytest.approx(1 / 2)
    assert summary["false_alarm_rate"] == pytest.approx(1 / 2)
    assert summary["missed_draft_rate"] == pytest.approx(0.5)
    assert summary["total_cost"] == pytest.approx(0.14)


def test_rates_with_nothing_to_measure_are_none_not_a_crash():
    summary = summarize([])
    assert summary["tier_accuracy"] is None and summary["faithfulness_pass_rate"] is None


def test_the_faithfulness_summary_counts_sentences_and_drafts():
    results = [
        score_case(make_case("L", 0), good_state(faith=report_with("supported", "supported", "supported", "supported"))),
        score_case(make_case("L", 1), good_state(faith=report_with("supported", "supported", "unsupported", "supported"))),
    ]
    summary = summarize(results)
    assert summary["faithfulness_pass_rate"] == 0.5 and summary["sentences_checked"] == 8
    assert summary["sentences_supported_rate"] == pytest.approx(7 / 8) and summary["unsupported"] == 1


def test_the_markdown_report_has_the_headlines_and_marks_wrong_answers():
    results = [score_case(make_case("L", 0), good_state()), score_case(make_case("L", 1), good_state(tier="low"))]
    text = render_markdown(results, summarize(results), "My report")
    assert text.startswith("# My report") and "Tier accuracy: **50%**" in text
    assert "| ACC-L-00001 | medium | low (!)" in text and "| ACC-L-00000 | medium | medium |" in text


# ===================================================================== fault planting
def test_planting_adds_invented_sentences_a_contradiction_and_a_reversal_without_touching_the_original():
    original = narrative("Emma Wang is a Teacher.", WIRE)
    corrupted, planted = plant_faults(evidence(), original)
    kinds = [p.kind for p in planted]
    assert kinds == ["invented_motive", "invented_relationship", "invented_destination", "contradicted_pep", "reversed_direction"]
    texts = [s.text for s in corrupted.facts]
    assert all(p.text in texts for p in planted)
    assert "from ACC-LC-00000-0 to ACC-L-00000" in texts[1]  # the wire's direction was reversed in place
    assert [s.text for s in original.facts] == ["Emma Wang is a Teacher.", WIRE]  # the original is untouched
    assert planted[-1].replaced == WIRE


def test_the_pep_contradiction_says_the_opposite_of_the_profile():
    flagged = plant_faults(evidence(), narrative(WIRE))[1]
    assert next(p for p in flagged if p.kind == "contradicted_pep").text == "The account holder is not a politically exposed person."
    clean = evidence().model_copy(update={"profile_summary": "Tom Garcia is not flagged as PEP (Politically Exposed Person)."})
    assert next(p for p in plant_faults(clean, narrative(WIRE))[1] if p.kind == "contradicted_pep").text == (
        "The account holder is a politically exposed person."
    )


def test_a_draft_with_no_wire_sentence_gets_no_reversal():
    kinds = [p.kind for p in plant_faults(evidence(), narrative("Emma Wang is a Teacher."))[1]]
    assert "reversed_direction" not in kinds and len(kinds) == 4


def judge_for(corrupted, planted, *, catch):
    """A report as if a judge flagged the planted kinds in `catch`, and approved everything else."""
    by_text = {p.text: p for p in planted}
    checks = []
    for i, text in enumerate([s.text for part in (corrupted.introduction, corrupted.facts, corrupted.conclusion) for s in part], 1):
        fault = by_text.get(text)
        verdict = "unsupported" if fault and fault.kind in catch else "supported"
        checks.append(StatementCheck(number=i, section="body", text=text, verdict=verdict))
    return FaithfulnessReport(account_id=ACCOUNT, checks=checks)


def test_scoring_a_judge_counts_what_it_caught_missed_and_wrongly_questioned():
    corrupted, planted = plant_faults(evidence(), narrative("Emma Wang is a Teacher.", WIRE))
    report = judge_for(corrupted, planted, catch={"invented_motive", "invented_destination", "contradicted_pep"})
    report.checks[0] = report.checks[0].model_copy(update={"verdict": "unsupported"})  # a genuine sentence wrongly flagged
    score = score_judge(report, planted)
    assert (score.planted, score.caught_by_judge, score.missed) == (5, 3, 2)
    assert sorted(score.missed_kinds) == ["invented_relationship", "reversed_direction"]
    assert score.recall == pytest.approx(3 / 5) and score.genuine_flagged == 1 and score.false_positive_rate is not None


def test_an_unverified_verdict_counts_as_caught_by_the_verifier_not_the_judge():
    corrupted, planted = plant_faults(evidence(), narrative(WIRE))
    report = judge_for(corrupted, planted, catch=set())
    report.checks[1] = report.checks[1].model_copy(update={"verdict": "unverified"})
    score = score_judge(report, planted)
    assert score.caught_by_verifier == 1 and score.caught_by_judge == 0


# ========================================================================= the loops
def test_evaluate_grades_every_case_and_reports_progress_after_each():
    cases = sample_cases({"N": 1, "S": 1, "L": 1, "H": 0})
    seen = []
    results, states = evaluate(cases, lambda case: (good_state(), 0.01), BudgetMeter(5.0),
                               on_result=lambda result, state: seen.append(result.case.account_id))
    assert [r.case.account_id for r in results] == seen == list(states)
    assert all(r.cost == 0.01 for r in results)


def test_evaluate_stops_before_the_cap_is_passed_and_keeps_what_it_finished():
    cases = sample_cases({"N": 0, "S": 5, "L": 0, "H": 0})
    done = []
    meter = BudgetMeter(0.20)  # each flagged case is estimated at 0.065 x 1.5 of headroom
    with pytest.raises(BudgetExceededError, match="Stopping before evaluating"):
        evaluate(cases, lambda case: (good_state(), 0.06), meter, on_result=lambda r, s: done.append(r))
    assert 0 < len(done) < 5 and meter.spent <= meter.cap


def test_the_estimate_adds_faithfulness_only_for_accounts_that_get_a_draft():
    cases = sample_cases({"N": 2, "S": 1, "L": 0, "H": 1})
    assert estimate_cost(cases, with_faithfulness=False) == pytest.approx(2 * 0.025 + 0.05 + 0.075)
    assert estimate_cost(cases) == pytest.approx(estimate_cost(cases, False) + 2 * 0.015)


def test_only_drafted_runs_are_fact_checked():
    states = {"a": good_state(), "b": {"status": "no_action", "narrative": SarNarrative(account_id="b", status="not_drafted")}}
    assert list(drafted_states(states)) == ["a"]


def perfect_judge(prompt):
    """Flags exactly the sentences the planter invents/contradicts/reverses; approves the rest."""
    invented = ("deliberately in order to avoid", "close business associate", "luxury vehicle", "not a politically exposed",
                "from ACC-LC-00000-0 to ACC-L-00000")
    rulings = []
    for number, text in re.findall(r"^(\d+)\. \[\w+\] (.*)$", prompt, flags=re.MULTILINE):
        bad = any(marker in text for marker in invented)
        rulings.append(JudgedStatement(number=int(number), verdict="unsupported" if bad else "supported",
                                       evidence_quote="" if bad else f"Account: {ACCOUNT}"))
    return FaithfulnessJudgment(statements=rulings)


def lazy_judge(prompt):
    return FaithfulnessJudgment(statements=[
        JudgedStatement(number=int(n), verdict="supported", evidence_quote=f"Account: {ACCOUNT}")
        for n in re.findall(r"^(\d+)\. \[", prompt, flags=re.MULTILINE)
    ])


def test_compare_judges_shows_a_good_judge_catching_everything_and_a_lazy_one_missing_it():
    states = {ACCOUNT: good_state()}
    scores = compare_judges(states, ["good", "lazy"], Settings(_env_file=None), BudgetMeter(5.0),
                            judge_factory=lambda s: perfect_judge if s.faithfulness_model == "good" else lazy_judge)
    assert scores["good"][0].recall == 1.0 and scores["good"][0].genuine_flagged == 0
    assert scores["lazy"][0].recall == 0.0 and len(scores["lazy"][0].missed_kinds) == 5


def test_apply_faithfulness_attaches_a_verified_report_to_each_drafted_run():
    states = {ACCOUNT: good_state()}
    count = apply_faithfulness(states, "any", Settings(_env_file=None), BudgetMeter(5.0), judge_factory=lambda s: lazy_judge)
    assert count == 1 and states[ACCOUNT]["faithfulness"].supported == 4 and states[ACCOUNT]["faithfulness"].passed


def test_check_faithfulness_import_is_the_real_function():
    assert callable(check_faithfulness)


# ====================================================================== command line
def test_parse_per_family():
    assert parse_per_family("N=6, s=2,H=0") == {"N": 6, "S": 2, "H": 0}


# ------------------------------------------- one unusable judge answer does not stop the run
def schema_slip_judge(prompt):
    from aml_copilot.errors import ModelOutputError

    raise ModelOutputError("did not match the required schema at 'statements'")


def test_a_bad_judge_answer_on_one_draft_is_skipped_but_the_next_draft_is_still_checked():
    from aml_copilot.errors import ModelOutputError
    from aml_copilot.evaluation.runner import apply_faithfulness

    calls = []

    def flaky_judge(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise ModelOutputError("slip")
        return lazy_judge(prompt)

    states = {"first": good_state(), "second": good_state()}
    problems: list[str] = []
    count = apply_faithfulness(states, "m", Settings(_env_file=None), BudgetMeter(5.0), lambda s: flaky_judge, problems)
    assert count == 1 and "faithfulness" not in states["first"] and "faithfulness" in states["second"]
    assert states["first"]["faithfulness_error"] == "slip" and problems == ["first: slip"]


def test_the_spending_cap_still_stops_the_loop_even_though_judge_errors_are_tolerated():
    from aml_copilot.evaluation.runner import apply_faithfulness

    meter = BudgetMeter(0.01)  # less than the safety-margined estimate for one check
    with pytest.raises(BudgetExceededError):
        apply_faithfulness({"a": good_state()}, "m", Settings(_env_file=None), meter, lambda s: lazy_judge)


def test_compare_judges_skips_a_draft_with_an_unusable_answer_and_reports_it():
    answers = []

    def judge(prompt):
        answers.append(1)
        if len(answers) == 1:
            return schema_slip_judge(prompt)
        return perfect_judge(prompt)

    problems: list[str] = []
    scores = compare_judges({"a": good_state(), "b": good_state()}, ["m"], Settings(_env_file=None), BudgetMeter(5.0),
                            judge_factory=lambda s: judge, errors=problems)
    assert len(scores["m"]) == 1 and len(problems) == 1 and problems[0].startswith("m on a:")


def test_only_missing_checks_just_the_drafts_without_a_report():
    from aml_copilot.evaluation.runner import apply_faithfulness

    done = good_state(faith=report_with("supported"))
    states = {"done": done, "todo": good_state()}
    seen = []

    def judge(prompt):
        seen.append(prompt)
        return lazy_judge(prompt)

    count = apply_faithfulness(states, "m", Settings(_env_file=None), BudgetMeter(5.0), lambda s: judge, only_missing=True)
    assert count == 1 and len(seen) == 1 and states["done"]["faithfulness"] is done["faithfulness"]


# ---- the six real wordings the planter used to get wrong (from the live evaluation's profiles)
@pytest.mark.parametrize(("profile", "is_pep"), [
    ("PEP status: not a politically exposed person.", False),
    ("Sofia Smith, occupation: Retail manager, risk rating: Medium, not a PEP, KYC last reviewed 2026-01-15.", False),
    ("She is not a PEP.", False),
    ("Torres is not a politically exposed person (PEP).", False),
    ("The account is not flagged as PEP (Politically Exposed Person).", False),
    ("Carlos Rodriguez, freelance contractor, high risk rating, not a PEP, KYC last reviewed 2025-07-30.", False),
    ("Sofia Torres is listed as an Accountant with a High risk rating and PEP (Politically Exposed Person) status.", True),
    ("Wei Chen is a real estate agent with PEP (politically exposed person) status.", True),
    ("Tom Garcia is a small business owner with a Medium risk rating.", False),  # no mention at all
])
def test_the_planter_reads_every_way_the_evidence_words_pep_status(profile, is_pep):
    from aml_copilot.evaluation.judge_check import pep_status

    assert pep_status(profile) is is_pep


def test_the_planted_pep_sentence_is_always_false_for_the_profile_it_is_planted_in():
    for profile, claim in (
        ("She is not a PEP.", "The account holder is a politically exposed person."),
        ("Emma has PEP (politically exposed person) status.", "The account holder is not a politically exposed person."),
        ("Tom is a small business owner.", "The account holder is a politically exposed person."),
    ):
        _, planted = plant_faults(evidence().model_copy(update={"profile_summary": profile}), narrative(WIRE))
        assert next(p for p in planted if p.kind == "contradicted_pep").text == claim


# ================================================================ scheduled canary + result sink
def test_a_healthy_run_has_no_regressions_and_each_kind_of_miss_is_named():
    from aml_copilot.evaluation.metrics import regressions

    healthy = score_case(make_case("L", 0), good_state())
    assert regressions([healthy]) == []
    wrong_tier = score_case(make_case("L", 0), good_state(tier="low"))
    failed = score_case(make_case("S", 1), {"status": "failed", "error": "boom"})
    assert any("risk tier was low" in p for p in regressions([wrong_tier]))
    assert any("the run failed (boom)" in p for p in regressions([failed]))


def test_one_flagged_draft_is_normal_but_a_drop_in_supported_sentences_is_a_regression():
    from aml_copilot.evaluation.metrics import regressions

    mostly_fine = ["supported"] * 9 + ["unsupported"]  # 90% - exactly at the minimum, still healthy
    flagged_once = score_case(make_case("L", 0), good_state(faith=report_with(*mostly_fine)))
    assert regressions([flagged_once]) == []
    degraded = score_case(make_case("L", 0), good_state(faith=report_with("supported", "unsupported", "unsupported")))
    assert any("of draft sentences were supported" in p for p in regressions([degraded]))


def sink_info():
    from aml_copilot.evaluation.sinks import RunInfo

    return RunInfo("run1", "2026-10-11T12:00:00+00:00", "canary", "abc123", "anthropic")


def test_a_result_becomes_a_complete_row_with_the_run_details():
    from aml_copilot.evaluation.sinks import COLUMNS, result_row

    row = result_row(score_case(make_case("L", 0), good_state(), seconds=4.26, cost=0.04567), sink_info())
    assert set(row) == set(COLUMNS)
    assert row["account_id"] == "ACC-L-00000" and row["tier_ok"] is True and row["cost_usd"] == 0.0457
    assert row["seconds"] == 4.3 and row["git_sha"] == "abc123" and row["kind"] == "canary"


def test_the_delta_sink_inserts_one_parameterised_row_per_result():
    from aml_copilot.evaluation.sinks import COLUMNS, DeltaSink, result_row

    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, statement, params):
            executed.append((statement, params))

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    rows = [result_row(score_case(make_case("L", n), good_state()), sink_info()) for n in range(2)]
    DeltaSink(Connection, "workspace.aml_copilot.eval_runs").write(rows)
    assert len(executed) == 2
    statement, params = executed[0]
    assert statement.startswith("INSERT INTO `workspace`.`aml_copilot`.`eval_runs`")
    assert "CAST(:run_at AS TIMESTAMP)" in statement and set(params) == set(COLUMNS)


@pytest.mark.parametrize("name", ["t; DROP TABLE x", "a.b.c.d", "bad-name", ""])
def test_the_delta_sink_refuses_a_table_name_that_is_not_plain(name):
    from aml_copilot.evaluation.sinks import DeltaSink

    with pytest.raises(ValueError):
        DeltaSink(lambda: None, name)


def run_canary(monkeypatch, state_for, capsys):
    from aml_copilot.evaluation import __main__ as cli

    monkeypatch.setattr(cli, "live_run_fn", lambda settings, with_faithfulness: lambda case: (state_for(case), 0.05))
    code = cli.main(["x", "canary", "--yes", "--per-family", "L=1", "--cap", "1.0"])
    return code, capsys.readouterr().out


def test_the_canary_exits_zero_when_healthy(monkeypatch, capsys):
    code, out = run_canary(monkeypatch, lambda case: good_state(), capsys)
    assert code == 0 and "HEALTHY" in out and "Stored 1 rows" in out


def test_the_canary_exits_one_and_names_the_problem_on_a_regression(monkeypatch, capsys):
    code, out = run_canary(monkeypatch, lambda case: good_state(tier="low"), capsys)
    assert code == 1 and "REGRESSION: ACC-L-" in out and "REGRESSION DETECTED" in out


def test_the_canary_without_yes_spends_nothing(monkeypatch, capsys):
    from aml_copilot.evaluation import __main__ as cli

    monkeypatch.setattr(cli, "live_run_fn", lambda *a: pytest.fail("must not run"))
    assert cli.main(["x", "canary", "--per-family", "L=1"]) == 0
    assert "Nothing was run" in capsys.readouterr().out
