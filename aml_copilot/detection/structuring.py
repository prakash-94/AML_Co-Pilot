"""Structuring detector: flags cash deposits clustered just under the CTR reporting threshold.

Structuring ("smurfing") is breaking one large sum into several smaller cash deposits, each
under the $10,000 threshold that would otherwise trigger a mandatory Currency Transaction
Report, often spread across branches or days to avoid the report. This looks only at the
account's OWN transactions - the Layering Detector, not this one, follows money to other
accounts.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from aml_copilot.detection.common import parse_timestamp

CTR_THRESHOLD = 10_000.0


@dataclass(frozen=True)
class StructuringConfig:
    threshold: float = CTR_THRESHOLD
    near_threshold_floor: float = 0.7 * CTR_THRESHOLD  # how close to the limit counts as "near"
    min_deposits: int = 3  # this many near-threshold deposits...
    # 90 days (one quarter), not a handful of days: live data showed a real structuring account
    # spreading its deposits ~52 days apart across three branches specifically to stay under a
    # short-window detector - a 7-day default missed it entirely. 90 days is also a common
    # real-world AML monitoring lookback.
    window_days: int = 90
    transaction_type: str = "cash_deposit"


@dataclass
class StructuringSignal:
    flagged: bool
    matched_transaction_ids: list[str] = field(default_factory=list)
    total_amount: float = 0.0
    distinct_branches: list[str] = field(default_factory=list)
    window_days: int | None = None
    reason: str = ""


def detect_structuring(
    transactions: list[dict], config: StructuringConfig | None = None
) -> StructuringSignal:
    cfg = config or StructuringConfig()

    candidates = [
        t
        for t in transactions
        if str(t.get("type")) == cfg.transaction_type
        and cfg.near_threshold_floor <= float(t.get("amount", 0)) < cfg.threshold
    ]
    if len(candidates) < cfg.min_deposits:
        return StructuringSignal(flagged=False, reason="Fewer than the minimum near-threshold deposits found")

    dated = sorted(
        ((parse_timestamp(t["timestamp"]), t) for t in candidates if t.get("timestamp") is not None),
        key=lambda pair: pair[0],
    )

    window = timedelta(days=cfg.window_days)
    best: list[dict] | None = None
    left = 0
    for right in range(len(dated)):
        while dated[right][0] - dated[left][0] > window:
            left += 1
        group = [t for _, t in dated[left : right + 1]]
        if len(group) >= cfg.min_deposits and (best is None or len(group) > len(best)):
            best = group

    if best is None:
        return StructuringSignal(
            flagged=False, reason="Near-threshold deposits found but not clustered within the window"
        )

    branches = sorted({str(t.get("branch_id")) for t in best if t.get("branch_id") is not None})
    return StructuringSignal(
        flagged=True,
        matched_transaction_ids=[str(t.get("transaction_id")) for t in best],
        total_amount=sum(float(t.get("amount", 0)) for t in best),
        distinct_branches=branches,
        window_days=cfg.window_days,
        reason=(
            f"{len(best)} cash deposits between ${cfg.near_threshold_floor:,.0f} and "
            f"${cfg.threshold:,.0f} within a {cfg.window_days}-day window"
            + (f", across {len(branches)} branches" if len(branches) > 1 else "")
        ),
    )
