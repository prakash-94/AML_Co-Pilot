"""Ingestion pipeline: raw ingestion -> integration layer -> normalization -> entity resolution."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import structlog

from aml_copilot.config import Settings
from aml_copilot.errors import SourceUnavailableError
from aml_copilot.ids import validate_account_id
from aml_copilot.ingestion.expansion import ExpansionConfig, expand_network
from aml_copilot.ingestion.models import EvidenceBundle
from aml_copilot.ingestion.normalize import (
    normalize_cases,
    normalize_profile,
    normalize_transactions,
    resolve_entity,
)
from aml_copilot.ingestion.sources import (
    DatabricksSource,
    FetchResult,
    SalesforceSource,
    SupabaseSource,
)

log = structlog.get_logger()

DEFAULT_EXPANSION = ExpansionConfig()


def _safe_fetch(source, account_id: str) -> tuple[FetchResult | None, str | None]:
    """Run one source; an outage becomes a recorded gap, not a crash."""
    try:
        return source.fetch(account_id), None
    except SourceUnavailableError as exc:
        log.warning("source_unavailable", source=source.name, error=str(exc))
        return None, source.name


@dataclass
class RawEvidence:
    """Integration layer output: the three raw pulls merged, not yet normalized."""

    account_id: str
    transaction_rows: list[dict[str, Any]]
    profile_rows: list[dict[str, Any]]
    case_rows: list[dict[str, Any]]
    profile_queried: bool  # False when Salesforce was down, so "no rows" means "unknown"
    missing_sources: list[str]
    warnings: list[str]


def merge_sources(
    account_id: str,
    tx: FetchResult | None,
    prof: FetchResult | None,
    cases: FetchResult | None,
) -> RawEvidence:
    """Integration layer: combine the raw pulls into one record for the account."""
    results = (tx, prof, cases)
    names = ("databricks", "salesforce", "supabase")
    return RawEvidence(
        account_id=account_id,
        transaction_rows=tx.rows if tx else [],
        profile_rows=prof.rows if prof else [],
        case_rows=cases.rows if cases else [],
        profile_queried=prof is not None,
        missing_sources=[n for n, r in zip(names, results, strict=True) if r is None],
        warnings=[w for r in results if r for w in r.warnings],
    )


def run_ingestion(
    account_id: str,
    *,
    transactions_source,
    profile_source,
    cases_source,
    expansion: ExpansionConfig | None = None,
) -> EvidenceBundle:
    """Run the four ingestion steps, then (when `expansion` is given) network expansion."""
    account_id = validate_account_id(account_id)

    # 1. Raw data ingestion - the three systems are queried concurrently.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_safe_fetch, src, account_id)
            for src in (transactions_source, profile_source, cases_source)
        ]
        (tx, _), (prof, _), (cases, _) = (f.result() for f in futures)

    # 2. Integration layer - merge the three pulls into one record.
    raw = merge_sources(account_id, tx, prof, cases)
    missing, warnings = raw.missing_sources, raw.warnings

    # 3. Schema normalization - map every source onto one internal schema.
    transactions = normalize_transactions(raw.transaction_rows)
    profile = normalize_profile(raw.profile_rows)
    case_history = normalize_cases(raw.case_rows)
    if raw.profile_queried and profile is None:
        warnings.append(f"No Salesforce KYC profile found for {account_id}")

    # 4. Entity resolution - every record must belong to the requested account.
    resolve_entity(account_id, transactions, profile, case_history)

    # 5. Network expansion - linked accounts stay separate from the account's own rows, so the
    #    entity check above (every primary row belongs to this account) is unaffected.
    net = None
    if expansion is not None and "databricks" not in missing:
        fetch_around = getattr(transactions_source, "fetch_around", None)
        if fetch_around is not None:
            net = expand_network(account_id, fetch_around, expansion)
            warnings.extend(net.warnings)
    elif expansion is not None:
        warnings.append("Network expansion skipped: databricks unavailable")

    log.info(
        "ingestion_complete",
        account_id=account_id,
        transactions=len(transactions),
        related_transactions=len(net.related_transactions) if net else 0,
        related_accounts=len(net.related_accounts) if net else 0,
        cases=len(case_history),
        has_profile=profile is not None,
        missing_sources=missing,
    )
    return EvidenceBundle(
        account_id=account_id,
        transactions=transactions,
        related_transactions=net.related_transactions if net else [],
        related_accounts=net.related_accounts if net else [],
        network_expanded=net is not None,
        profile=profile,
        case_history=case_history,
        missing_sources=missing,
        warnings=warnings,
    )


def ingest(
    account_id: str,
    settings: Settings | None = None,
    expansion: ExpansionConfig | None = DEFAULT_EXPANSION,
) -> EvidenceBundle:
    """Run ingestion against the real Databricks, Salesforce and Supabase sources.

    Network expansion is on by default; pass `expansion=None` for the account's own rows only.
    """
    settings = settings or Settings()
    return run_ingestion(
        account_id,
        transactions_source=DatabricksSource(settings),
        profile_source=SalesforceSource(settings),
        cases_source=SupabaseSource(settings),
        expansion=expansion,
    )
