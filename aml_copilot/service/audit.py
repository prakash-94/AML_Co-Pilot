"""The audit trail: an append-only record of everything that happened to a job.

Each event is written once and never changed. Every event also carries the hash of the event
before it in the same job, so the events form a chain: if anyone edits, deletes or reorders an
earlier event afterwards, `verify_chain` finds the break. (Tamper-EVIDENCE, not tamper-proofing -
someone with full control of the storage could rewrite the whole chain; the bucket's retention
policy is the other half of the protection.)

Storage is reached through `ObjectBackend`: "create this object only if it does not exist yet".
Cloud Storage does that with an `ifGenerationMatch=0` precondition; the in-memory backend below
does it with a lock, which is what the tests use.
"""

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from aml_copilot.errors import AMLCopilotError

EventType = Literal["JOB_SUBMITTED", "RUN_STARTED", "RUN_FINISHED", "RUN_FAILED", "DECISION"]
GENESIS_HASH = "0" * 64  # the "previous hash" of a job's first event
APPEND_ATTEMPTS = 5


class ObjectExistsError(AMLCopilotError):
    """`put_if_absent` found an object already stored under that name."""


class AuditChainError(AMLCopilotError):
    """The audit trail failed verification."""


class AuditEvent(BaseModel):
    job_id: str
    seq: int  # 1, 2, 3 ... within the job
    event_type: EventType
    at: str  # ISO timestamp
    actor: str  # a user id, or "system"
    details: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    event_hash: str = ""


def compute_event_hash(event: AuditEvent) -> str:
    canonical = json.dumps(event.model_dump(mode="json", exclude={"event_hash"}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _object_name(job_id: str, seq: int) -> str:
    return f"audit/{job_id}/{seq:06d}.json"


# ------------------------------------------------------------------------------ the storage
class ObjectBackend(Protocol):
    def put_if_absent(self, name: str, data: str) -> None:
        """Store `data` under `name`; raise ObjectExistsError if that name is already taken."""

    def list_names(self, prefix: str) -> list[str]: ...

    def read(self, name: str) -> str: ...


class InMemoryObjects:
    def __init__(self) -> None:
        self._objects: dict[str, str] = {}
        self._lock = threading.Lock()

    def put_if_absent(self, name: str, data: str) -> None:
        with self._lock:
            if name in self._objects:
                raise ObjectExistsError(name)
            self._objects[name] = data

    def list_names(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(n for n in self._objects if n.startswith(prefix))

    def read(self, name: str) -> str:
        with self._lock:
            return self._objects[name]


# --------------------------------------------------------------------------------- the log
@dataclass(frozen=True)
class ChainReport:
    ok: bool
    events: int
    problem: str | None = None


class AuditLog:
    def __init__(self, objects: ObjectBackend):
        self._objects = objects

    def events(self, job_id: str) -> list[AuditEvent]:
        names = self._objects.list_names(f"audit/{job_id}/")
        return [AuditEvent.model_validate_json(self._objects.read(n)) for n in names]

    def append(self, job_id: str, event_type: EventType, *, at: str, actor: str,
               details: dict[str, Any] | None = None) -> AuditEvent:
        """Add the next event to the job's chain. If another writer took the same sequence number
        first, re-read the chain and try the next one - so events are never overwritten or lost."""
        for _ in range(APPEND_ATTEMPTS):
            existing = self.events(job_id)
            previous = existing[-1] if existing else None
            event = AuditEvent(
                job_id=job_id, seq=(previous.seq + 1) if previous else 1, event_type=event_type, at=at,
                actor=actor, details=details or {}, prev_hash=previous.event_hash if previous else GENESIS_HASH,
            )
            event = event.model_copy(update={"event_hash": compute_event_hash(event)})
            try:
                self._objects.put_if_absent(_object_name(job_id, event.seq), event.model_dump_json())
                return event
            except ObjectExistsError:
                continue
        raise AuditChainError(f"Could not append to the audit trail of {job_id} after {APPEND_ATTEMPTS} tries")

    def verify_chain(self, job_id: str) -> ChainReport:
        """Check every event: numbered 1..n without gaps, hash matches its content, links to the
        one before. Reports the first problem found."""
        events = self.events(job_id)
        expected_prev = GENESIS_HASH
        for index, event in enumerate(events, start=1):
            if event.seq != index:
                return ChainReport(False, len(events), f"event {index} is numbered {event.seq} (an event is missing or moved)")
            if event.prev_hash != expected_prev:
                return ChainReport(False, len(events), f"event {index} does not follow the event before it")
            if compute_event_hash(event) != event.event_hash:
                return ChainReport(False, len(events), f"event {index} was changed after it was written")
            expected_prev = event.event_hash
        return ChainReport(True, len(events))


__all__ = [
    "GENESIS_HASH", "AuditChainError", "AuditEvent", "AuditLog", "ChainReport", "InMemoryObjects",
    "ObjectBackend", "ObjectExistsError", "compute_event_hash",
]
