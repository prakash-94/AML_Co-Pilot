"""Command line for the evaluation. Every command that calls Claude has a spending cap (--cap).

    python -m aml_copilot.evaluation run          # show the plan and cost estimate (spends nothing)
    python -m aml_copilot.evaluation run --yes    # run the pipeline on the sample accounts
    python -m aml_copilot.evaluation judges --yes # which model is the better fact-checker?
    python -m aml_copilot.evaluation faithfulness --model claude-haiku-4-5 --yes
    python -m aml_copilot.evaluation report       # grade the saved runs; spends nothing
    python -m aml_copilot.evaluation canary --yes --sink delta   # the scheduled health check

Runs are saved to evaluation_results/states.pkl after every account, so a stopped run loses nothing.
"""

import argparse
import pickle
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aml_copilot.config import Settings
from aml_copilot.errors import AMLCopilotError
from aml_copilot.evaluation.cost import BudgetMeter
from aml_copilot.evaluation.dataset import DEFAULT_PER_FAMILY, EvalCase, sample_cases
from aml_copilot.evaluation.metrics import regressions, render_markdown, score_case, summarize
from aml_copilot.evaluation.runner import (
    apply_faithfulness,
    compare_judges,
    estimate_cost,
    evaluate,
    live_run_fn,
)
from aml_copilot.evaluation.sinks import DeltaSink, InMemorySink, ResultSink, RunInfo, result_row
from aml_copilot.logging_config import configure_logging_from_settings

RESULTS_DIR = Path(__file__).resolve().parents[2] / "evaluation_results"
DEFAULT_STATES = RESULTS_DIR / "states.pkl"
DEFAULT_JUDGES = "claude-haiku-4-5,claude-sonnet-5"


def parse_per_family(text: str) -> dict[str, int]:
    """'N=6,S=6,L=6,H=3' -> {'N': 6, 'S': 6, 'L': 6, 'H': 3}"""
    counts = {}
    for part in text.split(","):
        family, _, number = part.partition("=")
        counts[family.strip().upper()] = int(number)
    return counts


def _load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"No saved runs at {path} - run `python -m aml_copilot.evaluation run --yes` first")
    with open(path, "rb") as fh:
        return pickle.load(fh)  # our own file, written by _save


def _save(path: Path, saved: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        pickle.dump(saved, fh)


def cmd_run(args: argparse.Namespace) -> None:
    cases = sample_cases(parse_per_family(args.per_family), args.seed)
    with_faithfulness = not args.no_faithfulness
    print(f"{len(cases)} accounts (seed {args.seed}): " + ", ".join(c.account_id for c in cases))
    print(f"Estimated cost: about ${estimate_cost(cases, with_faithfulness):.2f}; hard cap ${args.cap:.2f}")
    if not args.yes:
        print("Nothing was run. Add --yes to spend it.")
        return

    saved = {"cases": cases, "states": {}}
    meter = BudgetMeter(args.cap)

    def progress(result, state):
        saved["states"][result.case.account_id] = state
        saved.setdefault("meta", {})[result.case.account_id] = {"cost": result.cost, "seconds": result.seconds}
        _save(args.states, saved)
        print(f"  {result.case.account_id}: {result.status}, tier {result.tier}, drafted {result.drafted}, "
              f"${result.cost:.3f} (total ${meter.spent:.3f})", flush=True)

    try:
        evaluate(cases, live_run_fn(Settings(), with_faithfulness), meter, with_faithfulness, progress)
    except AMLCopilotError as exc:
        print(f"STOPPED: {exc}")
    print(f"Spent ${meter.spent:.3f}. Saved to {args.states}")


def make_sink(name: str, settings: Settings, table: str) -> ResultSink:
    """`delta` writes over the SQL connector (a laptop, CI, Cloud Run); `spark` uses the job's own Spark session."""
    if name == "delta":
        from aml_copilot.ingestion.sources import DatabricksSource
        from aml_copilot.service.delta import connector_sql

        return DeltaSink(connector_sql(DatabricksSource(settings)._open), table)
    if name == "spark":
        from databricks.sdk.runtime import spark

        from aml_copilot.service.delta import spark_sql

        return DeltaSink(spark_sql(spark), table)
    return InMemorySink()


def cmd_canary(args: argparse.Namespace) -> int:
    """A small scheduled health check: run the pipeline on a few accounts, store the results, and fail
    (exit code 1) if anything regressed - the failed job is what raises the alert."""
    settings = Settings()
    cases = sample_cases(parse_per_family(args.per_family), args.seed)
    print(f"Canary on {len(cases)} accounts: " + ", ".join(c.account_id for c in cases))
    print(f"Estimated cost: about ${estimate_cost(cases, True):.2f}; hard cap ${args.cap:.2f}")
    if not args.yes:
        print("Nothing was run. Add --yes to spend it.")
        return 0
    meter = BudgetMeter(args.cap)
    results: list = []
    try:
        results, _ = evaluate(cases, live_run_fn(settings, True), meter, True,
                              lambda r, s: print(f"  {r.case.account_id}: {r.status}, tier {r.tier}, ${r.cost:.3f}", flush=True))
    except AMLCopilotError as exc:
        print(f"STOPPED: {exc}")
    info = RunInfo(uuid.uuid4().hex, datetime.now(UTC).isoformat(), "canary", settings.build_sha or "unknown",
                   settings.llm_provider)
    try:
        make_sink(args.sink, settings, args.table).write([result_row(r, info) for r in results])
        print(f"Stored {len(results)} rows ({args.sink}) as run {info.run_id}")
    except Exception as exc:  # noqa: BLE001  (a storage problem must not hide the quality verdict below)
        print(f"WARNING: could not store results: {type(exc).__name__}")
    problems = regressions(results)
    if len(results) < len(cases):
        problems.append(f"only {len(results)} of {len(cases)} accounts were evaluated")
    for problem in problems:
        print(f"REGRESSION: {problem}")
    print(f"Spent ${meter.spent:.3f}. {'HEALTHY' if not problems else 'REGRESSION DETECTED'}")
    return 1 if problems else 0


def cmd_judges(args: argparse.Namespace) -> None:
    saved = _load(args.states)
    models = [m.strip() for m in args.models.split(",")]
    meter = BudgetMeter(args.cap)
    problems: list[str] = []
    print(f"Planting errors in {len(saved['states'])} saved runs and judging with {models}; cap ${args.cap:.2f}")
    if not args.yes:
        print("Nothing was run. Add --yes to spend it.")
        return
    try:
        scores = compare_judges(saved["states"], models, Settings(), meter, errors=problems)
    except AMLCopilotError as exc:
        print(f"STOPPED: {exc}")
        return
    print(f"\n{'model':22} {'drafts':>6} {'planted':>7} {'caught':>7} {'recall':>7} {'false alarms':>13}")
    for model, rows in scores.items():
        planted = sum(r.planted for r in rows)
        caught = sum(r.caught_by_judge + r.caught_by_verifier for r in rows)
        genuine = sum(r.genuine for r in rows)
        flagged = sum(r.genuine_flagged for r in rows)
        missed = [kind for r in rows for kind in r.missed_kinds]
        print(f"{model:22} {len(rows):>6} {planted:>7} {caught:>7} {caught / planted:>6.0%} "
              f"{flagged:>6}/{genuine:<6}" + (f"  missed: {sorted(set(missed))}" if missed else ""))
    for problem in problems:
        print(f"  skipped - {problem}")
    print(f"\nSpent ${meter.spent:.3f}")


def cmd_faithfulness(args: argparse.Namespace) -> None:
    saved = _load(args.states)
    meter = BudgetMeter(args.cap)
    problems: list[str] = []
    print(f"Fact-checking the real drafts with {args.model}; cap ${args.cap:.2f}")
    if not args.yes:
        print("Nothing was run. Add --yes to spend it.")
        return
    try:
        count = apply_faithfulness(
            saved["states"], args.model, Settings(), meter, errors=problems, only_missing=args.only_missing
        )
    except AMLCopilotError as exc:
        print(f"STOPPED: {exc}")
        count = sum(1 for s in saved["states"].values() if "faithfulness" in s)
    _save(args.states, saved)
    for problem in problems:
        print(f"  could not check - {problem}")
    print(f"Checked {count} drafts. Spent ${meter.spent:.3f}")


def cmd_report(args: argparse.Namespace) -> None:
    saved = _load(args.states)
    cases: list[EvalCase] = saved["cases"]
    meta = saved.get("meta", {})
    results = [
        score_case(case, saved["states"][case.account_id], **meta.get(case.account_id, {}))
        for case in cases
        if case.account_id in saved["states"]
    ]
    report = render_markdown(results, summarize(results), args.title)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(report)


def main(argv: list[str]) -> int:
    configure_logging_from_settings()
    parser = argparse.ArgumentParser(prog="python -m aml_copilot.evaluation", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the pipeline on a fixed sample of accounts")
    run.add_argument("--per-family", default=",".join(f"{k}={v}" for k, v in DEFAULT_PER_FAMILY.items()))
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--cap", type=float, default=1.5, help="hard spending cap in dollars")
    run.add_argument("--no-faithfulness", action="store_true")
    run.add_argument("--yes", action="store_true", help="actually spend the money")
    run.set_defaults(handler=cmd_run)

    canary = sub.add_parser("canary", help="scheduled health check: a few accounts, results stored, exit 1 on regression")
    canary.add_argument("--per-family", default="N=2,S=2,L=2,H=1")
    canary.add_argument("--seed", type=int, default=7)
    canary.add_argument("--cap", type=float, default=0.6)
    canary.add_argument("--sink", choices=["none", "delta", "spark"], default="none")
    canary.add_argument("--table", default="workspace.aml_copilot.eval_runs")
    canary.add_argument("--yes", action="store_true")
    canary.set_defaults(handler=cmd_canary)

    judges = sub.add_parser("judges", help="test which model is the better fact-checker, using planted errors")
    judges.add_argument("--models", default=DEFAULT_JUDGES)
    judges.add_argument("--cap", type=float, default=0.6)
    judges.add_argument("--yes", action="store_true")
    judges.set_defaults(handler=cmd_judges)

    faith = sub.add_parser("faithfulness", help="fact-check the saved real drafts")
    faith.add_argument("--model", required=True)
    faith.add_argument("--only-missing", action="store_true", help="skip drafts that already have a report")
    faith.add_argument("--cap", type=float, default=0.4)
    faith.add_argument("--yes", action="store_true")
    faith.set_defaults(handler=cmd_faithfulness)

    report = sub.add_parser("report", help="grade the saved runs (free)")
    report.add_argument("--title", default="Evaluation report")
    report.add_argument("--out", default="")
    report.set_defaults(handler=cmd_report)

    for command in (run, judges, faith, report):
        command.add_argument("--states", type=Path, default=DEFAULT_STATES)

    args = parser.parse_args(argv[1:])
    return args.handler(args) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
