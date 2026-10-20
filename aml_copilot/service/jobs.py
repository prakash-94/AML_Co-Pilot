"""Jobs: one record per requested investigation, and the rules that keep them honest.

The rules (all enforced here, in plain Python, whatever database sits underneath):

* a job runs ONCE      - `claim` moves QUEUED -> RUNNING for exactly one caller, so Cloud Tasks'
                         at-least-once delivery can never start the same paid run twice;
* a decision is FINAL  - `decide` works only on a finished draft that is still PENDING_REVIEW, and
                         only if the reviewer's `result_hash` matches the stored draft;
* a day has a LIMIT    - `submit` counts jobs per UTC day and refuses past the limit (the real brake
                         on spend, since cloud budgets only alert);
* no DOUBLE-CLICKS     - while an investigation of an account is open, asking again returns it.

The database is reached only through `DocumentBackend.transact`: "read these documents, let me
decide, then write these documents - all or nothing". Firestore does that with a transaction; the
in-memory backend below does it with a lock, which is what the tests use.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from aml_copilot.errors import AMLCopilotError
from aml_copilot.service.models import Decision, InvestigationResult

JobStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "FAILED_TO_START"]
ReviewStatus = Literal["NOT_REQUIRED", "PENDING_REVIEW", "APPROVED", "REJECTED", "ESCALATED"]

JOBS, QUOTA, LOCKS = "jobs", "quota", "open_accounts"
_DECISION_TO_REVIEW: dict[str, ReviewStatus] = {"approve": "APPROVED", "reject": "REJECTED", "escalate": "ESCALATED"}
MAX_ERROR_CHARS = 500


class JobNotFoundError(AMLCopilotError):
    """No job has that id."""


class DecisionConflictError(AMLCopilotError):
    """The job cannot be decided now: it is not finished, needs no review, or was already decided."""


class StaleDraftError(AMLCopilotError):
    """The reviewer's `result_hash` is not the stored draft's - they approved something else."""


class DecisionRecord(BaseModel):
    decision: Decision
    comment: str
    result_hash: str
    decided_by: str  # the signed-in user's id
    decided_by_email: str = ""
    role: str
    decided_at: str


class JobRecord(BaseModel):
    job_id: str
    account_id: str
    status: JobStatus = "QUEUED"
    review_status: ReviewStatus = "NOT_REQUIRED"
    created_by: str
    created_by_email: str = ""
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: InvestigationResult | None = None
    error: str | None = None
    decision: DecisionRecord | None = None
    request_id: str | None = None
    provider: str = ""  # which model family answered: "gemini" or "claude" (blank for older records)


# ------------------------------------------------------------------------------ the database
Key = tuple[str, str]  # (collection, document id)
TransactFn = Callable[[dict[Key, dict | None]], dict[Key, dict]]


class DocumentBackend(Protocol):
    def get(self, collection: str, doc_id: str) -> dict | None: ...

    def transact(self, keys: list[Key], fn: TransactFn) -> None:
        """Atomically: read `keys`, call `fn(documents)`, and write what it returns (key -> new
        document). If `fn` raises, nothing is written and the error propagates."""

    def list_recent(self, collection: str, order_by: str, limit: int) -> list[dict]: ...


class InMemoryBackend:
    """A dictionary and a lock. Same contract as Firestore's transaction, minus the network."""

    def __init__(self) -> None:
        self._docs: dict[Key, dict] = {}
        self._lock = threading.RLock()

    def get(self, collection: str, doc_id: str) -> dict | None:
        with self._lock:
            doc = self._docs.get((collection, doc_id))
            return dict(doc) if doc is not None else None

    def transact(self, keys: list[Key], fn: TransactFn) -> None:
        with self._lock:
            writes = fn({key: (dict(self._docs[key]) if key in self._docs else None) for key in keys})
            self._docs.update({key: dict(doc) for key, doc in writes.items()})

    def list_recent(self, collection: str, order_by: str, limit: int) -> list[dict]:
        with self._lock:
            docs = [dict(d) for (col, _), d in self._docs.items() if col == collection]
        return sorted(docs, key=lambda d: d.get(order_by, ""), reverse=True)[:limit]


# ----------------------------------------------------------------------------- the rules
@dataclass(frozen=True)
class SubmitOutcome:
    kind: Literal["created", "duplicate", "quota_exceeded"]
    job_id: str | None = None  # the new job, or the already-open one for "duplicate"


class JobStore:
    def __init__(self, backend: DocumentBackend):
        self._db = backend

    # ---- reading
    def get(self, job_id: str) -> JobRecord | None:
        data = self._db.get(JOBS, job_id)
        return JobRecord.model_validate(data) if data is not None else None

    def list_recent(self, limit: int = 20) -> list[JobRecord]:
        return [JobRecord.model_validate(d) for d in self._db.list_recent(JOBS, "created_at", limit)]

    # ---- starting
    def submit(self, job: JobRecord, *, now: datetime, daily_limit: int, dedupe_minutes: int = 10) -> SubmitOutcome:
        """Create the job unless an investigation of this account is already open or today's quota is
        used up - decided atomically, so two simultaneous requests cannot both slip through."""
        keys = [(JOBS, job.job_id), (QUOTA, now.date().isoformat()), (LOCKS, job.account_id)]
        outcome: list[SubmitOutcome] = []

        def decide(docs: dict[Key, dict | None]) -> dict[Key, dict]:
            outcome.clear()  # a database may call this again if its transaction retries
            quota, lock = docs[keys[1]], docs[keys[2]]
            if lock and datetime.fromisoformat(lock["expires_at"]) > now:
                outcome.append(SubmitOutcome("duplicate", lock["job_id"]))
                return {}
            used = quota["count"] if quota else 0
            if used >= daily_limit:
                outcome.append(SubmitOutcome("quota_exceeded"))
                return {}
            outcome.append(SubmitOutcome("created", job.job_id))
            return {
                keys[0]: job.model_dump(mode="json"),
                keys[1]: {"count": used + 1},
                keys[2]: {"job_id": job.job_id, "expires_at": (now + timedelta(minutes=dedupe_minutes)).isoformat()},
            }

        self._db.transact(keys, decide)
        return outcome[0]

    # ---- running
    def claim(self, job_id: str, *, now: datetime) -> JobRecord | None:
        """QUEUED -> RUNNING for exactly one caller. Anyone else (a duplicate delivery) gets None."""
        claimed: list[JobRecord] = []

        def decide(docs: dict[Key, dict | None]) -> dict[Key, dict]:
            claimed.clear()
            doc = docs[(JOBS, job_id)]
            if doc is None or doc["status"] != "QUEUED":
                return {}
            doc = {**doc, "status": "RUNNING", "started_at": now.isoformat()}
            claimed.append(JobRecord.model_validate(doc))
            return {(JOBS, job_id): doc}

        self._db.transact([(JOBS, job_id)], decide)
        return claimed[0] if claimed else None

    def complete(self, job_id: str, account_id: str, result: InvestigationResult, *, now: datetime) -> None:
        review: ReviewStatus = "PENDING_REVIEW" if result.status == "draft_ready" else "NOT_REQUIRED"
        self._finish(job_id, account_id, now, status="COMPLETED", review_status=review,
                     result=result.model_dump(mode="json"), error=None)

    def fail(self, job_id: str, account_id: str, error: str, *, now: datetime, status: JobStatus = "FAILED") -> None:
        self._finish(job_id, account_id, now, status=status, review_status="NOT_REQUIRED",
                     result=None, error=error[:MAX_ERROR_CHARS])

    def _finish(self, job_id: str, account_id: str, now: datetime, **fields: Any) -> None:
        keys = [(JOBS, job_id), (LOCKS, account_id)]

        def decide(docs: dict[Key, dict | None]) -> dict[Key, dict]:
            doc = docs[keys[0]]
            if doc is None:
                raise JobNotFoundError(f"No job {job_id}")
            lock = docs[keys[1]]
            writes = {keys[0]: {**doc, **fields, "finished_at": now.isoformat()}}
            if lock and lock.get("job_id") == job_id:  # release the "open" marker so the account can be re-run
                writes[keys[1]] = {**lock, "expires_at": now.isoformat()}
            return writes

        self._db.transact(keys, decide)

    # ---- deciding
    def decide(self, job_id: str, record: DecisionRecord) -> JobRecord:
        """Record the reviewer's final decision. Raises if it is not decidable or the draft changed."""
        saved: list[JobRecord] = []

        def decide(docs: dict[Key, dict | None]) -> dict[Key, dict]:
            saved.clear()
            doc = docs[(JOBS, job_id)]
            if doc is None:
                raise JobNotFoundError(f"No job {job_id}")
            if doc["status"] != "COMPLETED" or doc["review_status"] != "PENDING_REVIEW":
                raise DecisionConflictError(
                    f"Job {job_id} cannot be decided now (status {doc['status']}, review {doc['review_status']})"
                )
            if doc["result"]["result_hash"] != record.result_hash:
                raise StaleDraftError("The decision refers to a different draft than the one stored")
            doc = {**doc, "review_status": _DECISION_TO_REVIEW[record.decision], "decision": record.model_dump(mode="json")}
            saved.append(JobRecord.model_validate(doc))
            return {(JOBS, job_id): doc}

        self._db.transact([(JOBS, job_id)], decide)
        return saved[0]


def new_job(job_id: str, account_id: str, *, created_by: str, created_by_email: str = "", now: datetime,
            request_id: str | None = None, provider: str = "") -> JobRecord:
    return JobRecord(job_id=job_id, account_id=account_id, created_by=created_by,
                     created_by_email=created_by_email, created_at=now.isoformat(), request_id=request_id,
                     provider=provider)


__all__ = [
    "DecisionConflictError", "DecisionRecord", "DocumentBackend", "InMemoryBackend", "JobNotFoundError",
    "JobRecord", "JobStore", "StaleDraftError", "SubmitOutcome", "new_job",
]
