"""Steps 3 and 4 - schema normalization and entity resolution."""

from typing import Any

from aml_copilot.errors import EntityResolutionError
from aml_copilot.ids import canonical
from aml_copilot.ingestion.models import CaseRecord, CustomerProfile
from aml_copilot.ingestion.sources import GROUND_TRUTH_COLUMNS


def normalize_transactions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lower-case keys; strip ground-truth columns again as defence in depth."""
    out = []
    for row in rows:
        clean = {k.lower(): v for k, v in row.items()}
        for col in GROUND_TRUTH_COLUMNS:
            clean.pop(col, None)
        out.append(clean)
    return out


def normalize_profile(records: list[dict[str, Any]]) -> CustomerProfile | None:
    """Map Salesforce field names onto the internal schema.

    No record means the customer is unknown to the CRM (None); more than one is
    ambiguous and cannot be resolved to a single entity.
    """
    if not records:
        return None
    if len(records) > 1:
        ids = sorted({canonical(r.get("Account_Id__c")) for r in records})
        raise EntityResolutionError(f"{len(records)} Salesforce profiles matched: {ids}")
    r = records[0]
    return CustomerProfile(
        account_id=canonical(r.get("Account_Id__c")),
        name=r.get("Name"),
        occupation=r.get("Occupation__c"),
        risk_rating=r.get("Risk_Rating__c"),
        is_pep=bool(r.get("Is_PEP__c")),
        kyc_last_reviewed=r.get("KYC_Last_Reviewed__c"),
    )


def normalize_cases(rows: list[dict[str, Any]]) -> list[CaseRecord]:
    return [CaseRecord(**{**r, "account_id": canonical(r.get("account_id"))}) for r in rows]


def resolve_entity(
    expected_id: str,
    transactions: list[dict[str, Any]],
    profile: CustomerProfile | None,
    cases: list[CaseRecord],
) -> None:
    """Confirm every record belongs to `expected_id`; raise on any mismatch."""
    found = {
        "databricks": {canonical(t.get("account_id")) for t in transactions},
        "salesforce": {profile.account_id} if profile else set(),
        "supabase": {c.account_id for c in cases},
    }
    bad = {src: sorted(ids - {expected_id}) for src, ids in found.items() if ids - {expected_id}}
    if bad:
        raise EntityResolutionError(f"Expected {expected_id}, but found other ids: {bad}")
