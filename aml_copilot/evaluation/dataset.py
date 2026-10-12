"""Which accounts to evaluate on, and what the right answer is for each.

The right answers come from the synthetic data's own design (the id prefix says which pattern the
account was generated with). They are used ONLY here, to grade the system afterwards - the agents
never see them.
"""

import random
from dataclasses import dataclass

# family: (expected risk tier, typology documents that should match)
EXPECTED = {
    "N": ("low", frozenset()),
    "S": ("medium", frozenset({"structuring.md"})),
    "L": ("medium", frozenset({"rapid_wire_transfers.md"})),
    "H": ("high", frozenset({"structuring.md", "rapid_wire_transfers.md"})),
}
# Accounts 0..n-1 of each family have a Salesforce KYC profile (the rest exist only in Databricks).
# Sampling only from those keeps every evaluated account's evidence complete.
PROFILED_ACCOUNTS = {"S": 840, "L": 630, "N": 630, "H": 3}
DEFAULT_PER_FAMILY = {"N": 6, "S": 6, "L": 6, "H": 3}


@dataclass(frozen=True)
class EvalCase:
    account_id: str
    family: str
    expected_tier: str
    expected_sources: frozenset

    @property
    def expects_draft(self) -> bool:
        return self.expected_tier != "low"


def make_case(family: str, number: int) -> EvalCase:
    tier, sources = EXPECTED[family]
    return EvalCase(f"ACC-{family}-{number:05d}", family, tier, sources)


def sample_cases(per_family: dict[str, int] | None = None, seed: int = 42) -> list[EvalCase]:
    """A fixed, reproducible sample: the same seed always picks the same accounts, so two runs of
    the evaluation (before and after a change) are comparable."""
    per_family = per_family or DEFAULT_PER_FAMILY
    rng = random.Random(seed)
    cases: list[EvalCase] = []
    for family in ("N", "S", "L", "H"):  # a fixed order, whatever order the dict came in
        count = per_family.get(family, 0)
        available = PROFILED_ACCOUNTS[family]
        if count > available:
            raise ValueError(f"Only {available} profiled {family} accounts exist, asked for {count}")
        cases.extend(make_case(family, number) for number in sorted(rng.sample(range(available), count)))
    return cases
