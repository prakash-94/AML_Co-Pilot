"""Guardrails: checks that run AFTER the agents have written something.

`faithfulness` asks Claude to fact-check each narrative sentence against the evidence (and verifies
the judge's own quotes in plain code); `consistency` is the free, deterministic counterpart.
"""

from aml_copilot.guardrails.consistency import check_consistency
from aml_copilot.guardrails.faithfulness import (
    FaithfulnessConfig,
    FaithfulnessJudgment,
    FaithfulnessReport,
    StatementCheck,
    build_faithfulness_prompt,
    check_faithfulness,
    finalize_faithfulness,
    make_anthropic_faithfulness_judge,
    run_faithfulness,
)

__all__ = [
    "FaithfulnessConfig",
    "FaithfulnessJudgment",
    "FaithfulnessReport",
    "StatementCheck",
    "build_faithfulness_prompt",
    "check_consistency",
    "check_faithfulness",
    "finalize_faithfulness",
    "make_anthropic_faithfulness_judge",
    "run_faithfulness",
]
