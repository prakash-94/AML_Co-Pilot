"""One investigation = one job run.

The same service rules as the Cloud Run version, minus the web layer: submit (daily limit + one open job per
account), audit `JOB_SUBMITTED`, run the pipeline through `process_job` (claim -> run -> store the draft without raw
rows -> audit), and report the outcome. The investigate job allows ONE run at a time, so submit/finish never race.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from aml_copilot.ids import validate_account_id
from aml_copilot.service.audit import AuditLog
from aml_copilot.service.jobs import JobStore, new_job
from aml_copilot.service.models import Provenance
from aml_copilot.service.worker import RunFn, process_job

Outcome = Literal["ran", "failed", "duplicate", "quota_exceeded"]


@dataclass(frozen=True)
class InvestigationReport:
    outcome: Outcome
    job_id: str | None
    detail: str

    @property
    def ok(self) -> bool:
        """A duplicate is not an error (the open job answers the request); a refused or failed run is."""
        return self.outcome in ("ran", "duplicate")


def investigate(
    account_id: str, requested_by: str, *, store: JobStore, audit: AuditLog, run: RunFn,
    clock: Callable[[], datetime], daily_limit: int, provenance: Provenance | None = None,
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> InvestigationReport:
    account_id = validate_account_id(account_id)  # rejects anything that is not ACC-[S|L|N|H]-nnnnn
    now = clock()
    job = new_job(new_id(), account_id, created_by=requested_by, created_by_email=requested_by, now=now)
    submitted = store.submit(job, now=now, daily_limit=daily_limit)

    if submitted.kind == "duplicate":
        return InvestigationReport("duplicate", submitted.job_id, f"An investigation of {account_id} is already open")
    if submitted.kind == "quota_exceeded":
        return InvestigationReport("quota_exceeded", None, f"The daily limit of {daily_limit} investigations is reached")

    audit.append(job.job_id, "JOB_SUBMITTED", at=now.isoformat(), actor=requested_by,
                 details={"account_id": account_id, "via": "databricks job"})
    outcome = process_job(job.job_id, store=store, audit=audit, run=run, clock=clock, provenance=provenance)
    stored = store.get(job.job_id)
    if outcome != "ran" or stored is None or stored.result is None:
        return InvestigationReport("failed", job.job_id, (stored.error if stored else None) or "the run did not complete")
    result = stored.result
    return InvestigationReport("ran", job.job_id, f"{result.status}; review {stored.review_status}; hash {result.result_hash}")
