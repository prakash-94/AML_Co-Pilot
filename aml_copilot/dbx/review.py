"""The human decision, as run from a Databricks notebook.

The notebook runs as the person who opened it, so `current_user()` is a real, authenticated identity - no
typed-in name to forge. Their role comes from the `reviewers` table; only a supervisor may approve.
"""

from collections.abc import Callable
from datetime import datetime

from aml_copilot.errors import AMLCopilotError
from aml_copilot.service.audit import AuditLog
from aml_copilot.service.auth import ForbiddenError
from aml_copilot.service.jobs import DecisionRecord, JobNotFoundError, JobRecord, JobStore
from aml_copilot.service.models import DecisionRequest

RoleLookup = Callable[[str], str | None]
ACTIONS = ("list", "show", "approve", "reject", "escalate")


class ReviewError(AMLCopilotError):
    """The review could not be done; the message says why in plain words."""


def list_recent(store: JobStore, limit: int = 15) -> str:
    lines = [f"{'job id':34} {'account':12} {'status':10} {'review':15} created"]
    for job in store.list_recent(limit):
        lines.append(f"{job.job_id:34} {job.account_id:12} {job.status:10} {job.review_status:15} {job.created_at[:19]}")
    return "\n".join(lines)


def _find(store: JobStore, job_id: str) -> JobRecord:
    job = store.get(job_id.strip())
    if job is None:
        raise JobNotFoundError(f"No investigation {job_id!r}")
    return job


def show(store: JobStore, audit: AuditLog, job_id: str) -> str:
    job = _find(store, job_id)
    out = [f"Investigation {job.job_id}  account {job.account_id}  status {job.status}  review {job.review_status}"]
    result = job.result
    if result is None:
        out.append(job.error or "No result yet.")
    else:
        tier = (result.risk_score or {}).get("tier", "?")
        out += [f"Result: {result.status} (risk tier {tier})  hash {result.result_hash}", result.status_detail, ""]
        out.append(result.narrative_text or "(no draft)")
        if result.faithfulness:
            f = result.faithfulness
            out.append(f"\nFact check: {f.supported} supported, {f.unsupported} unsupported, {f.contradicted} contradicted, "
                       f"{f.unverified} unverified - {'PASSED' if f.passed else 'NEEDS ATTENTION'}")
            out += [f"  sentence {c.number} ({c.verdict}): {c.text} - {c.reason}" for c in f.checks if c.verdict != "supported"]
        out += [f"Warning: {w}" for w in result.warnings]
    if job.decision:
        d = job.decision
        out.append(f"\nDecision: {d.decision} by {d.decided_by} ({d.role}) - {d.comment}")
    report = audit.verify_chain(job.job_id)
    out.append("\nAudit trail " + ("verified" if report.ok else f"BROKEN: {report.problem}"))
    out += [f"  #{e.seq} {e.event_type} by {e.actor} at {e.at[:19]}" for e in audit.events(job.job_id)]
    return "\n".join(out)


def decide(
    store: JobStore, audit: AuditLog, roles: RoleLookup, job_id: str, decision: str, comment: str,
    user: str, now: datetime,
) -> str:
    role = roles(user)
    if role not in ("investigator", "supervisor"):
        raise ForbiddenError(f"{user} is not listed in the reviewers table")
    if decision == "approve" and role != "supervisor":
        raise ForbiddenError("Only a supervisor may approve a draft")

    job = _find(store, job_id)
    if job.result is None:
        raise ReviewError("There is no draft to decide on yet")
    request = DecisionRequest(decision=decision, comment=comment, result_hash=job.result.result_hash)  # type: ignore[arg-type]
    record = DecisionRecord(
        decision=request.decision, comment=request.comment, result_hash=request.result_hash, decided_by=user,
        decided_by_email=user, role=role, decided_at=now.isoformat(),
    )
    decided = store.decide(job.job_id, record)  # raises if it was already decided or the draft changed
    audit.append(job.job_id, "DECISION", at=now.isoformat(), actor=user, details={
        "decision": request.decision, "comment": request.comment, "result_hash": request.result_hash, "role": role,
    })
    return f"Recorded: {decided.review_status} by {user} ({role}) on {job.job_id}"


def run_action(
    action: str, job_id: str, comment: str, user: str, now: datetime, *, store: JobStore, audit: AuditLog,
    roles: RoleLookup,
) -> str:
    """What the review notebook calls for each button."""
    if action == "list":
        return list_recent(store)
    if action == "show":
        return show(store, audit, job_id)
    if action in ("approve", "reject", "escalate"):
        return decide(store, audit, roles, job_id, action, comment, user, now)
    raise ReviewError(f"Unknown action {action!r}; use one of {', '.join(ACTIONS)}")
