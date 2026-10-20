"""The web page's server: start an investigation, read the draft, record the decision.

It runs as a Databricks App. The platform signs people in and tells the app who they are in the `X-Forwarded-Email`
header (the app is only reachable through that proxy), and the `reviewers` table says what each person may do:

    investigator   start investigations, read drafts, reject or escalate
    supervisor     all of that, plus approve

Starting an investigation does not run it here: the app asks Databricks to run the `aml-investigate` job (`RunLauncher`),
exactly as pressing "Run now" would, so the page and the job UI share one code path, one daily limit and one audit trail.
Everything else (the rules for deciding, the audit chain) is the same code the review notebook uses.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import field_validator

from aml_copilot.dbx.providers import DEFAULT_PROVIDER, PROVIDERS
from aml_copilot.dbx.review import ReviewError, decide
from aml_copilot.dbx.runtime import RoleLookup
from aml_copilot.errors import AMLCopilotError
from aml_copilot.service.audit import AuditLog
from aml_copilot.service.auth import ForbiddenError
from aml_copilot.service.jobs import (
    DecisionConflictError,
    JobNotFoundError,
    JobStore,
    StaleDraftError,
)
from aml_copilot.service.models import DecisionRequest, StartRequest

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "aml-copilot"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class RunLauncher(Protocol):
    def start(self, account_id: str, requested_by: str, provider: str = DEFAULT_PROVIDER) -> dict[str, Any]:
        """Ask Databricks to run the investigate job with this model family; returns {"run_id": ...}."""

    def status(self, run_id: int) -> dict[str, Any]:
        """The run's state: {"state": ..., "result": ..., "message": ...}."""


class StartBody(StartRequest):
    """What the page sends to start a run: the account (validated like everywhere else) and the model family."""

    provider: str = DEFAULT_PROVIDER

    @field_validator("provider")
    @classmethod
    def known_provider(cls, value: str) -> str:
        if value not in PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
        return value


class Person:
    def __init__(self, email: str, role: str):
        self.email, self.role = email, role


def create_app(
    *, store: JobStore, audit: AuditLog, roles: RoleLookup, launcher: RunLauncher, clock: Callable[[], datetime],
    dev_user: str = "", static_dir: Path = STATIC_DIR,
) -> FastAPI:
    app = FastAPI(title="AML Investigation Copilot", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        """Always fetch the page and its script fresh, so a new deploy shows up without a hard refresh."""
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

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

    @app.exception_handler(ReviewError)
    async def _review(_: Request, exc: ReviewError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def current_person(x_forwarded_email: Annotated[str | None, Header()] = None) -> Person:
        email = (x_forwarded_email or dev_user or "").strip()
        if not email:
            raise HTTPException(status_code=401, detail="Not signed in")
        role = roles(email)
        if role not in ("investigator", "supervisor"):
            raise ForbiddenError(f"{email} is not listed in the reviewers table")
        return Person(email, role)

    Me = Annotated[Person, Depends(current_person)]

    def require_same_origin(request: Request) -> None:
        """A browser page on another site cannot add this header to a cross-site request, so its presence shows the
        call came from our own page (protection against cross-site request forgery for the buttons that change data)."""
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(status_code=403, detail="Missing or wrong X-Requested-With header")

    def load(job_id: str):
        job = store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"No investigation {job_id}")
        return job

    # ------------------------------------------------------------------------------------------- the API
    @app.get("/api/me")
    def me(person: Me) -> dict[str, str]:
        return {"email": person.email, "role": person.role}

    @app.get("/api/investigations")
    def recent(person: Me, limit: int = 25) -> list[dict[str, Any]]:
        return [
            {"job_id": j.job_id, "account_id": j.account_id, "status": j.status, "review_status": j.review_status,
             "created_at": j.created_at, "created_by": j.created_by, "provider": j.provider,
             "outcome": j.result.status if j.result else None,
             "tier": (j.result.risk_score or {}).get("tier") if j.result else None}
            for j in store.list_recent(max(1, min(limit, 100)))
        ]

    @app.get("/api/investigations/{job_id}")
    def one(job_id: str, person: Me) -> dict[str, Any]:
        return load(job_id).model_dump(mode="json")

    @app.get("/api/investigations/{job_id}/audit")
    def trail(job_id: str, person: Me) -> dict[str, Any]:
        load(job_id)
        report = audit.verify_chain(job_id)
        return {"events": [e.model_dump(mode="json") for e in audit.events(job_id)],
                "chain_ok": report.ok, "chain_problem": report.problem}

    @app.post("/api/investigations", status_code=202, dependencies=[Depends(require_same_origin)])
    def start(body: StartBody, person: Me) -> dict[str, Any]:
        try:
            return launcher.start(body.account_id, person.email, body.provider)
        except AMLCopilotError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: int, person: Me) -> dict[str, Any]:
        return launcher.status(run_id)

    @app.post("/api/investigations/{job_id}/decision", dependencies=[Depends(require_same_origin)])
    def decision(job_id: str, body: DecisionRequest, person: Me) -> dict[str, str]:
        job = load(job_id)
        if job.result is not None and body.result_hash != job.result.result_hash:
            raise StaleDraftError("The decision refers to a different draft than the one stored")
        message = decide(store, audit, roles, job_id, body.decision, body.comment, person.email, clock())
        return {"message": message}

    # ---------------------------------------------------------------------------------------- the page
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


__all__ = ["RunLauncher", "create_app"]
