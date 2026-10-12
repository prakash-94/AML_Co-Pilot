"""Layering detector: flags money moving rapidly through a chain of linked accounts.

Layering hides money's origin by passing it through several accounts. Network expansion
(aml_copilot.ingestion.expansion) already finds the chain; this detector judges whether that
chain is "rapid" - a short span from the first hop to the last - and reports whether it
crosses a border or shrinks in value at each hop, both common in real layering schemes. Our
transaction data has no currency field, so "cross-border" (a different country per hop)
stands in for "cross-currency" here.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from itertools import pairwise

from aml_copilot.detection.common import parse_timestamp
from aml_copilot.ingestion import EvidenceBundle


@dataclass(frozen=True)
class LayeringConfig:
    min_hops: int = 2
    max_window: timedelta = timedelta(hours=72)


@dataclass
class LayeringSignal:
    flagged: bool
    chain_transaction_ids: list[str] = field(default_factory=list)
    hop_count: int = 0
    elapsed: timedelta | None = None
    crosses_border: bool = False
    shrinking_amounts: bool = False
    reason: str = ""


def detect_layering(bundle: EvidenceBundle, config: LayeringConfig | None = None) -> LayeringSignal:
    cfg = config or LayeringConfig()

    related_ids = {a.account_id for a in bundle.related_accounts}
    root_wires = [
        {**t, "hop": 1}
        for t in bundle.transactions
        if str(t.get("type")) == "wire_transfer" and str(t.get("counterparty_account")) in related_ids
    ]
    chain = sorted(root_wires + bundle.related_transactions, key=lambda t: t.get("hop", 0))

    if len(chain) < cfg.min_hops:
        return LayeringSignal(flagged=False, hop_count=len(chain), reason="Fewer hops than the minimum required")

    times = [parse_timestamp(t["timestamp"]) for t in chain if t.get("timestamp") is not None]
    elapsed = max(times) - min(times) if times else None
    if elapsed is None or elapsed > cfg.max_window:
        return LayeringSignal(
            flagged=False,
            hop_count=len(chain),
            elapsed=elapsed,
            reason=f"Chain spans longer than the {cfg.max_window} rapid-transfer window",
        )

    # Countries in the order the money visited them, each once: US, KY, KY -> [US, KY].
    route = list(dict.fromkeys(str(t["country"]) for t in chain if t.get("country") is not None))
    amounts = [float(t.get("amount", 0)) for t in chain]
    shrinking = all(a >= b for a, b in pairwise(amounts)) if len(amounts) > 1 else False

    # Every fact the detector established goes into the reason text - including the negative
    # one. The reason is what the downstream agents read, so a fact that only lives in a field
    # (crosses_border) is invisible to them and gets reported as "cannot be assessed".
    facts = [f"{len(chain)}-hop transfer chain completed in {elapsed}"]
    if shrinking:
        facts.append("amounts shrink at each hop")
    if route:
        facts.append(
            f"crosses borders ({' -> '.join(route)})" if len(route) > 1 else f"stays within one country ({route[0]})"
        )

    return LayeringSignal(
        flagged=True,
        chain_transaction_ids=[str(t.get("transaction_id")) for t in chain],
        hop_count=len(chain),
        elapsed=elapsed,
        crosses_border=len(route) > 1,
        shrinking_amounts=shrinking,
        reason="; ".join(facts),
    )
