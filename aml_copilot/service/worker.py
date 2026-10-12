"""Running one queued job: the one place an investigation actually executes.

`process_job` claims the job (so a duplicate start cannot run the paid pipeline twice), runs the injected pipeline,
stores the draft without raw rows and writes the audit events. It never raises: a crash is recorded on the job.

The pipeline itself is injected as `run` (a function from account id to the finished run state), so everything here
is tested without a database or a model.
"""

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from aml_copilot.service.audit import AuditLog
from aml_copilot.service.jobs import JobStore
from aml_copilot.service.models import Provenance, to_api_result

logger = logging.getLogger(__name__)

RunFn = Callable[[str], Mapping[str, Any]]
Outcome = Literal["ran", "skipped", "failed"]
SYSTEM_ACTOR = "system"


def process_job(
    job_id: str, *, store: JobStore, audit: AuditLog, run: RunFn, clock: Callable[[], datetime],
    provenance: Provenance | None = None,
) -> Outcome:
    """Run one queued job. Never raises: a crash inside the pipeline is recorded on the job, so the
    task is not retried (a retry would be a second paid run)."""
    claimed = store.claim(job_id, now=clock())
    if claimed is None:
        logger.info("job_skipped", extra={"job_id": job_id, "reason": "not queued"})
        return "skipped"

    audit.append(job_id, "RUN_STARTED", at=clock().isoformat(), actor=SYSTEM_ACTOR,
                 details={"account_id": claimed.account_id})
    started = time.monotonic()
    try:
        state = run(claimed.account_id)
        base = provenance or Provenance()
        result = to_api_result(state, base.model_copy(update={
            "step_seconds": {
                **base.step_seconds, **dict(state.get("step_seconds", {})),
                "total": round(time.monotonic() - started, 1),
            },
        }))
    except Exception as exc:  # the pipeline reports its own failures in the state; this is a real crash
        store.fail(job_id, claimed.account_id, f"{type(exc).__name__}: {exc}", now=clock())
        audit.append(job_id, "RUN_FAILED", at=clock().isoformat(), actor=SYSTEM_ACTOR,
                     details={"error_type": type(exc).__name__})
        logger.exception("job_crashed", extra={"job_id": job_id})
        return "failed"

    store.complete(job_id, claimed.account_id, result, now=clock())
    audit.append(job_id, "RUN_FINISHED", at=clock().isoformat(), actor=SYSTEM_ACTOR, details={
        "status": result.status, "result_hash": result.result_hash, "provenance": result.provenance.model_dump(),
    })
    logger.info("job_finished", extra={"job_id": job_id, "run_status": result.status})
    return "ran"


__all__ = ["RunFn", "process_job"]
