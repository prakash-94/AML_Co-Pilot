"""Running an evaluation: live model calls under a hard spending cap, graded by metrics.py.

The functions that spend money take their dependencies as arguments (`run_fn`, `judge_factory`),
so the budget logic and the scoring are testable with fakes; `live_run_fn` is the real one.
"""

import time
from collections.abc import Callable

from aml_copilot.config import Settings
from aml_copilot.errors import ModelOutputError
from aml_copilot.evaluation.cost import BudgetMeter, usage_cost
from aml_copilot.evaluation.dataset import EvalCase
from aml_copilot.evaluation.judge_check import JudgeScore, plant_faults, score_judge
from aml_copilot.evaluation.metrics import CaseResult, score_case

# Rough dollars per account, from the measured costs of the pipeline's three model calls (about
# 0.75 + 1.5 + 2 cents for a flagged account, less for a clean one). Used only to refuse to start
# a step that might overshoot the cap, and for the up-front estimate - never for the real totals.
PER_CASE_ESTIMATE = {"N": 0.025, "S": 0.05, "L": 0.05, "H": 0.075}
FAITHFULNESS_ESTIMATE = 0.015
SAFETY_MARGIN = 1.5  # require this multiple of the estimate to be available before each step

RunFn = Callable[[EvalCase], tuple[dict, float]]  # case -> (orchestrator state, dollars spent)


def estimate_cost(cases: list[EvalCase], with_faithfulness: bool = True) -> float:
    total = sum(PER_CASE_ESTIMATE[case.family] for case in cases)
    if with_faithfulness:
        total += FAITHFULNESS_ESTIMATE * sum(1 for case in cases if case.expects_draft)
    return total


def live_run_fn(settings: Settings, check_faithfulness: bool) -> RunFn:
    """The real thing: the whole pipeline against the live systems, with its cost measured."""
    from langchain_core.callbacks import get_usage_metadata_callback

    from aml_copilot.agents.investigation import run_investigation

    def run(case: EvalCase) -> tuple[dict, float]:
        with get_usage_metadata_callback() as callback:
            state = run_investigation(case.account_id, settings, check_faithfulness)
            cost = usage_cost(callback.usage_metadata)
        return state, cost

    return run


def evaluate(
    cases: list[EvalCase],
    run_fn: RunFn,
    meter: BudgetMeter,
    with_faithfulness: bool = True,
    on_result: Callable[[CaseResult, dict], None] | None = None,
) -> tuple[list[CaseResult], dict[str, dict]]:
    """Run every case, grading each; stops (raising BudgetExceededError) before the cap is passed.

    Returns the graded results and the raw run states, keyed by account id, for later reuse. If the
    cap stops the run early, `on_result` has already seen every account that did finish.
    """
    results: list[CaseResult] = []
    states: dict[str, dict] = {}
    for case in cases:
        estimate = PER_CASE_ESTIMATE[case.family] + (FAITHFULNESS_ESTIMATE if with_faithfulness else 0)
        meter.require(estimate * SAFETY_MARGIN, f"evaluating {case.account_id}")
        started = time.time()
        state, cost = run_fn(case)
        meter.add(cost)
        result = score_case(case, state, seconds=time.time() - started, cost=cost)
        results.append(result)
        states[case.account_id] = state
        if on_result:
            on_result(result, state)  # lets the caller save progress after every account
    return results, states


def drafted_states(states: dict[str, dict]) -> dict[str, dict]:
    """The runs that produced a narrative draft - the only ones a faithfulness check applies to."""
    return {
        account: state
        for account, state in states.items()
        if state.get("narrative") is not None and state["narrative"].status == "draft_for_human_review"
    }


def _measured(function: Callable, *args):
    """Call `function(*args)` and return (its result, the dollars the model calls inside it cost)."""
    from langchain_core.callbacks import get_usage_metadata_callback

    with get_usage_metadata_callback() as callback:
        value = function(*args)
    return value, usage_cost(callback.usage_metadata)


def compare_judges(
    states: dict[str, dict],
    models: list[str],
    settings: Settings,
    meter: BudgetMeter,
    judge_factory: Callable | None = None,
    errors: list[str] | None = None,
) -> dict[str, list[JudgeScore]]:
    """Plant known errors in each real draft and see which models' judges catch them.

    A draft whose judge answer is unusable (it did not fit the schema even after the retry) is
    skipped and noted in `errors`; the spending cap still stops everything.
    """
    from aml_copilot.guardrails import check_faithfulness, make_anthropic_faithfulness_judge

    judge_factory = judge_factory or make_anthropic_faithfulness_judge
    scores: dict[str, list[JudgeScore]] = {}
    for model in models:
        judge_fn = judge_factory(settings.model_copy(update={"faithfulness_model": model}))
        scores[model] = []
        for account, state in drafted_states(states).items():
            meter.require(FAITHFULNESS_ESTIMATE * SAFETY_MARGIN, f"judging {account} with {model}")
            corrupted, planted = plant_faults(state["evidence"], state["narrative"])
            try:
                report, cost = _measured(
                    check_faithfulness, state["evidence"], state["risk_score"], state["assessment"], corrupted, judge_fn
                )
            except ModelOutputError as exc:
                if errors is not None:
                    errors.append(f"{model} on {account}: {exc}")
                continue
            meter.add(cost)
            scores[model].append(score_judge(report, planted))
    return scores


def apply_faithfulness(
    states: dict[str, dict],
    model: str,
    settings: Settings,
    meter: BudgetMeter,
    judge_factory: Callable | None = None,
    errors: list[str] | None = None,
    only_missing: bool = False,
) -> int:
    """Fact-check every real draft with `model`'s judge, attaching the report to its run state.

    A draft whose judge answer is unusable is marked with `faithfulness_error` and noted in
    `errors`, and the loop carries on with the next one. With `only_missing`, drafts that already
    have a report are left alone (so a retry pays only for the ones that failed).
    """
    from aml_copilot.guardrails import check_faithfulness, make_anthropic_faithfulness_judge

    judge_factory = judge_factory or make_anthropic_faithfulness_judge
    judge_fn = judge_factory(settings.model_copy(update={"faithfulness_model": model}))
    checked = 0
    for account, state in drafted_states(states).items():
        if only_missing and "faithfulness" in state:
            continue
        meter.require(FAITHFULNESS_ESTIMATE * SAFETY_MARGIN, f"fact-checking {account}")
        try:
            report, cost = _measured(
                check_faithfulness, state["evidence"], state["risk_score"], state["assessment"], state["narrative"], judge_fn
            )
        except ModelOutputError as exc:
            state["faithfulness_error"] = str(exc)
            if errors is not None:
                errors.append(f"{account}: {exc}")
            continue
        meter.add(cost)
        state["faithfulness"] = report
        state.pop("faithfulness_error", None)
        checked += 1
    return checked
