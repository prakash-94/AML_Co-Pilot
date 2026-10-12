"""What the model calls cost, and a hard spending cap.

Prices are US dollars per million tokens (input, output), from Anthropic's published pricing.
"""

from collections.abc import Mapping
from typing import Any

from aml_copilot.errors import AMLCopilotError

# (input, output) per million tokens. Keys are matched as substrings of the model name the API
# reports, e.g. "claude-haiku-4-5-20251001" contains "haiku".
PRICE_PER_MILLION = {"haiku": (1.00, 5.00), "sonnet": (2.00, 10.00)}


class BudgetExceededError(AMLCopilotError):
    """The next step would take the run past its spending cap."""


def usage_cost(usage_metadata: Mapping[str, Mapping[str, Any]]) -> float:
    """Dollar cost of a LangChain usage report ({model name: {input_tokens, output_tokens, ...}})."""
    total = 0.0
    for model, usage in usage_metadata.items():
        family = next((key for key in PRICE_PER_MILLION if key in model), None)
        if family is None:
            raise ValueError(f"No price known for model {model!r}")
        price_in, price_out = PRICE_PER_MILLION[family]
        total += usage.get("input_tokens", 0) * price_in / 1e6 + usage.get("output_tokens", 0) * price_out / 1e6
    return total


class BudgetMeter:
    """Adds up spend and refuses to start a step that could overshoot the cap."""

    def __init__(self, cap: float):
        self.cap = cap
        self.spent = 0.0

    @property
    def remaining(self) -> float:
        return self.cap - self.spent

    def add(self, cost: float) -> None:
        self.spent += cost

    def require(self, estimate: float, what: str = "the next step") -> None:
        """Raise unless there is room for `estimate` dollars more."""
        if self.spent + estimate > self.cap:
            raise BudgetExceededError(
                f"Stopping before {what}: ${self.spent:.3f} spent of a ${self.cap:.2f} cap, "
                f"and it needs about ${estimate:.3f} more"
            )
