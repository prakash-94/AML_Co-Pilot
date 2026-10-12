"""The web API a browser talks to. Never runs the pipeline - it records the request and hands the job
to a launcher (Cloud Tasks in production); the worker does the work.

    GET  /status                         no sign-in needed
    POST /investigations                  start (or re-find) an investigation        -> 202 / 200 / 429
    GET  /investigations                  recent jobs
    GET  /investigations/{id}             one job: status, draft, review state
    POST /investigations/{id}/decision    approve (supervisor only) / reject / escalate
    GET  /investigations/{id}/audit       the audit trail and whether its chain verifies
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from aml_copilot.service.audit import AuditLog
from aml_copilot.service.auth import (
    AuthError,
    ForbiddenError,
    Principal,
    TokenVerifier,
    bearer_token,
    require_role,
)
from aml_copilot.service.jobs import (
    DecisionConflictError,
    DecisionRecord,
    JobNotFoundError,
    JobRecord,
    JobStore,
    StaleDraftError,
    new_job,
)
from aml_copilot.service.models import DecisionRequest, StartRequest


class JobLauncher(Protocol):
    def enqueue(self, job_id: str) -> None:
        """Hand the job to the worker. Raise if it could not be queued."""


def create_app(
    *, store: JobStore, audit: AuditLog, launcher: JobLauncher, verifier: TokenVerifier,
    clock: Callable[[], datetime], daily_limit: int = 30, build: dict[str, str] | None = None,
    id_factory: Callable[[], str] = lambda: uuid.uuid4().hex, prefix: str = "",
) -> FastAPI:
    app = FastAPI(title="AML Investigation Copilot", docs_url=None, redoc_url=None, openapi_url=None)

    # ---- turn our own errors into the right HTTP status
    @app.exception_handler(AuthError)
    async def _auth(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_: Request, exc: ForbiddenError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(JobNotFoundError)
    async def _missing(_: Request, exc: JobNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(DecisionConflictError)
    async def _conflict(_: Request, exc: DecisionConflictError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(StaleDraftError)
    async def _stale(_: Request, exc: StaleDraftError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def current_user(authorization: Annotated[str | None, Header()] = None) -> Principal:
        return verifier.verify(bearer_token(authorization))

    User = Annotated[Principal, Depends(current_user)]

    def load(job_id: str) -> JobRecord:
        job = store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"No job {job_id}")
        return job

    # ---- routes (Firebase Hosting forwards the whole path, so on Cloud Run they live under "/api")
    router = APIRouter()

    @app.get("/status")
    def status() -> dict:
        return {"status": "ok", "role": "api", "build": build or {}}

    @router.post("/investigations")
    def start(body: StartRequest, user: User) -> JSONResponse:
        now = clock()
        job = new_job(id_factory(), body.account_id, created_by=user.uid, created_by_email=user.email, now=now)
        outcome = store.submit(job, now=now, daily_limit=daily_limit)
        if outcome.kind == "quota_exceeded":
            return JSONResponse({"detail": "The daily investigation limit has been reached"}, status_code=429)
        if outcome.kind == "duplicate":
            return JSONResponse({"job_id": outcome.job_id, "duplicate": True}, status_code=200)

        audit.append(job.job_id, "JOB_SUBMITTED", at=now.isoformat(), actor=user.uid,
                     details={"account_id": job.account_id, "role": user.role})
        try:
            launcher.enqueue(job.job_id)
        except Exception as exc:
            store.fail(job.job_id, job.account_id, f"could not queue: {type(exc).__name__}", now=clock(),
                       status="FAILED_TO_START")
            audit.append(job.job_id, "RUN_FAILED", at=clock().isoformat(), actor="system",
                         details={"error_type": type(exc).__name__, "stage": "enqueue"})
            raise HTTPException(status_code=503, detail="The investigation could not be queued") from exc
        return JSONResponse({"job_id": job.job_id, "duplicate": False}, status_code=202)

    @router.get("/investigations")
    def recent(user: User, limit: int = 20) -> list[dict]:
        return [j.model_dump(mode="json") for j in store.list_recent(max(1, min(limit, 100)))]

    @router.get("/investigations/{job_id}")
    def one(job_id: str, user: User) -> dict:
        return load(job_id).model_dump(mode="json")

    @router.post("/investigations/{job_id}/decision")
    def decide(job_id: str, body: DecisionRequest, user: User) -> dict:
        require_role(user, "investigator", "supervisor")
        if body.decision == "approve":
            require_role(user, "supervisor")  # only a supervisor may approve a draft
        now = clock()
        record = DecisionRecord(
            decision=body.decision, comment=body.comment, result_hash=body.result_hash, decided_by=user.uid,
            decided_by_email=user.email, role=user.role, decided_at=now.isoformat(),
        )
        decided = store.decide(job_id, record)
        audit.append(job_id, "DECISION", at=now.isoformat(), actor=user.uid, details={
            "decision": body.decision, "comment": body.comment, "result_hash": body.result_hash, "role": user.role,
        })
        return decided.model_dump(mode="json")

    @router.get("/investigations/{job_id}/audit")
    def trail(job_id: str, user: User) -> dict:
        load(job_id)
        report = audit.verify_chain(job_id)
        return {
            "events": [e.model_dump(mode="json") for e in audit.events(job_id)],
            "chain_ok": report.ok, "chain_problem": report.problem,
        }

    app.include_router(router, prefix=prefix)
    return app


__all__ = ["JobLauncher", "create_app"]
