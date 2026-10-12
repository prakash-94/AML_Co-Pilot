"""Entry point: builds the right FastAPI app from settings.

    uvicorn aml_copilot.service.app:app --port 8080

SERVICE_ROLE=api     the browser-facing API (default)
SERVICE_ROLE=worker  the private endpoint Cloud Tasks calls; the only role that runs the pipeline

SERVICE_BACKEND=memory (default) keeps everything in this process for local development: dev tokens
(`Bearer dev-investigator` / `Bearer dev-supervisor`) and an in-process launcher that runs the job in a
background thread. SERVICE_BACKEND=gcp uses Firestore, Cloud Storage and Cloud Tasks.
"""

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from aml_copilot.config import Settings, require
from aml_copilot.errors import ConfigError
from aml_copilot.logging_config import configure_logging
from aml_copilot.service import auth
from aml_copilot.service.api import create_app
from aml_copilot.service.audit import AuditLog, InMemoryObjects
from aml_copilot.service.jobs import InMemoryBackend, JobStore
from aml_copilot.service.models import Provenance
from aml_copilot.service.worker import create_worker_app, process_job


def utc_now() -> datetime:
    return datetime.now(UTC)


def make_run_fn(settings: Settings) -> Callable[[str], Mapping[str, Any]]:
    """The real pipeline. Imported only here, so the api role never loads torch or the embedder."""

    def run(account_id: str) -> Mapping[str, Any]:
        from aml_copilot.agents.investigation import run_investigation

        timings: dict[str, float] = {}
        state = run_investigation(account_id, settings, observer=timings.__setitem__)
        return {**state, "step_seconds": timings}

    return run


def make_provenance(settings: Settings) -> Provenance:
    models = {
        "evidence": settings.anthropic_model,
        "typology": settings.typology_model or settings.anthropic_model,
        "narrative": settings.narrative_model or settings.anthropic_model,
        "faithfulness": settings.faithfulness_model or settings.anthropic_model,
    }
    return Provenance(llm_provider=settings.llm_provider, models=models, build={"sha": settings.build_sha})


class InProcessLauncher:
    """Local development: run the job in a background thread of this same process."""

    def __init__(self, run_job: Callable[[str], object]):
        self._run_job = run_job

    def enqueue(self, job_id: str) -> None:
        threading.Thread(target=self._run_job, args=(job_id,), daemon=True).start()


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_format, settings.log_level)

    if settings.service_backend == "gcp":
        require(settings, "audit_bucket")
        from aml_copilot.service.gcp import CloudTasksLauncher, FirestoreBackend, GcsObjects

        store = JobStore(FirestoreBackend(settings.gcp_project))
        audit_log = AuditLog(GcsObjects(settings.audit_bucket))
    elif settings.service_backend == "memory":
        store, audit_log = JobStore(InMemoryBackend()), AuditLog(InMemoryObjects())
    else:
        raise ConfigError(f"Unknown SERVICE_BACKEND '{settings.service_backend}' (use 'memory' or 'gcp')")

    if settings.service_role == "worker":
        if settings.service_backend != "gcp":
            raise ConfigError("The worker role needs SERVICE_BACKEND=gcp")
        return create_worker_app(
            store=store, audit=audit_log, run=make_run_fn(settings), clock=utc_now,
            verifier=auth.CloudTasksCallerVerifier(settings.worker_url, settings.tasks_invoker_email),
            provenance=make_provenance(settings),
        )

    if settings.service_role != "api":
        raise ConfigError(f"Unknown SERVICE_ROLE '{settings.service_role}' (use 'api' or 'worker')")

    if settings.service_backend == "gcp":
        require(settings, "gcp_project", "worker_url", "tasks_invoker_email")
        launcher = CloudTasksLauncher(settings.gcp_project, settings.tasks_location, settings.tasks_queue,
                                      settings.worker_url, settings.tasks_invoker_email)
    else:
        provenance = make_provenance(settings)
        run = make_run_fn(settings)
        launcher = InProcessLauncher(
            lambda job_id: process_job(job_id, store=store, audit=audit_log, run=run, clock=utc_now,
                                       provenance=provenance)
        )

    return create_app(
        store=store, audit=audit_log, launcher=launcher, clock=utc_now,
        verifier=auth.build_verifier(settings.auth_mode, firebase_project_id=settings.firebase_project_id),
        daily_limit=settings.daily_investigation_limit, build={"sha": settings.build_sha},
        prefix=settings.api_prefix,
    )


def __getattr__(name: str) -> FastAPI:
    """`uvicorn aml_copilot.service.app:app` builds the app on first use (so importing this module
    for tests does not read settings or open connections)."""
    if name == "app":
        return build_app()
    raise AttributeError(name)
