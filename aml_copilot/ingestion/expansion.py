"""Network expansion: follow transfer links out from the flagged account.

Layering hides in chains of accounts (ACC-L-00000 -> ACC-LC-00000-0 -> ACC-LC-00000-1 ...),
so the flagged account's own rows show only the first hop. This walks outward breadth-first
over `counterparty_account` links, in both directions, until it runs out of links or hits a
bound (hops, accounts, rows). Hitting a bound is reported, never silent.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from aml_copilot.errors import SourceUnavailableError
from aml_copilot.ids import canonical
from aml_copilot.ingestion.models import RelatedAccount
from aml_copilot.ingestion.normalize import normalize_transactions

log = structlog.get_logger()

# rows(account_ids, limit) -> transactions where any id is the sender or the counterparty
FetchAround = Callable[[Sequence[str], int], list[dict[str, Any]]]


@dataclass(frozen=True)
class ExpansionConfig:
    max_hops: int = 4
    max_accounts: int = 30  # related accounts, not counting the flagged one
    max_rows: int = 500  # related transactions kept, per query and overall
    follow_types: frozenset[str] = frozenset({"wire_transfer"})  # only these extend the chain


@dataclass
class Expansion:
    related_transactions: list[dict[str, Any]] = field(default_factory=list)
    related_accounts: list[RelatedAccount] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True  # False when a bound was hit or a query failed


def _row_key(row: dict[str, Any]) -> Any:
    if row.get("transaction_id") is not None:
        return row["transaction_id"]
    return tuple(row.get(k) for k in ("account_id", "counterparty_account", "timestamp", "amount"))


def expand_network(
    root: str,
    fetch_around: FetchAround,
    config: ExpansionConfig | None = None,
) -> Expansion:
    """Breadth-first expansion from `root`; returns only rows that are not the root's own."""
    cfg = config or ExpansionConfig()
    out = Expansion()
    visited: dict[str, int] = {root: 0}  # account id -> hop at which it was reached
    found: dict[Any, dict[str, Any]] = {}  # transaction key -> row, tagged with its hop
    frontier = [root]

    for hop in range(1, cfg.max_hops + 1):
        try:
            rows = normalize_transactions(fetch_around(frontier, cfg.max_rows + 1))
        except SourceUnavailableError as exc:
            log.warning("expansion_query_failed", hop=hop, error=str(exc))
            out.warnings.append(f"Network expansion stopped at hop {hop}: {exc}")
            out.complete = False
            break

        truncated = len(rows) > cfg.max_rows
        rows = rows[: cfg.max_rows]
        new_accounts: set[str] = set()
        for row in rows:
            found.setdefault(_row_key(row), {**row, "hop": hop})
            if str(row.get("type", "")).lower() in cfg.follow_types:
                for acct in (canonical(row.get("account_id")), canonical(row.get("counterparty_account"))):
                    if acct and acct not in visited:
                        new_accounts.add(acct)

        room = cfg.max_accounts - (len(visited) - 1)
        ordered = sorted(new_accounts)  # deterministic when a cap applies
        if len(ordered) > room:
            out.warnings.append(
                f"Network expansion capped at {cfg.max_accounts} related accounts; "
                f"{len(ordered) - room} more were left unexpanded"
            )
            out.complete = False
            ordered = ordered[:room]
        for acct in ordered:
            visited[acct] = hop

        if truncated:
            out.warnings.append(f"Network expansion truncated to {cfg.max_rows} rows at hop {hop}")
            out.complete = False
            break
        frontier = ordered
        if not frontier:
            break
    else:
        if frontier:
            out.warnings.append(
                f"Network expansion stopped at {cfg.max_hops} hops; "
                f"{len(frontier)} account(s) were not expanded further"
            )
            out.complete = False

    out.related_accounts = [RelatedAccount(account_id=a, hop=h) for a, h in visited.items() if a != root]
    out.related_transactions = [r for r in found.values() if canonical(r.get("account_id")) != root]
    return out
