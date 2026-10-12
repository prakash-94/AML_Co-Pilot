"""Normalized evidence models handed to the Evidence Agent."""

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class CustomerProfile(BaseModel):
    account_id: str
    name: str | None = None
    occupation: str | None = None
    risk_rating: str | None = None
    is_pep: bool = False
    kyc_last_reviewed: date | None = None


class CaseRecord(BaseModel):
    case_id: str | int
    account_id: str
    case_type: str | None = None
    opened_date: date | None = None
    closed_date: date | None = None
    status: str | None = None
    disposition: str | None = None
    investigator_notes: str | None = None


class RelatedAccount(BaseModel):
    """An account linked to the flagged one through transfers; `hop` is its distance."""

    account_id: str
    hop: int


class EvidenceBundle(BaseModel):
    """Everything known about one account, joined on account_id.

    `transactions` are the flagged account's own rows. `related_transactions` and
    `related_accounts` come from network expansion: transfers to and from linked accounts,
    each row tagged with the `hop` at which it was found. Layering chains live there.

    `missing_sources` lists systems that could not be queried at all;
    `warnings` carries softer issues (no KYC profile found, truncated results).
    Downstream agents must treat a non-empty `missing_sources` as partial evidence.
    """

    account_id: str
    transactions: list[dict[str, Any]] = Field(default_factory=list)
    related_transactions: list[dict[str, Any]] = Field(default_factory=list)
    related_accounts: list[RelatedAccount] = Field(default_factory=list)
    network_expanded: bool = False
    profile: CustomerProfile | None = None
    case_history: list[CaseRecord] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
