"""The API and worker together, on in-memory fakes: sign-in, roles, one-shot decisions, audit, no raw rows."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from aml_copilot.agents.models import NarrativeStatement, SarNarrative
from aml_copilot.ingestion import EvidenceBundle
from aml_copilot.service.api import create_app
from aml_copilot.service.audit import AuditLog, InMemoryObjects
from aml_copilot.service.auth import AllowAnyCaller, AuthError, StaticTokenVerifier, dev_people
from aml_copilot.service.jobs import InMemoryBackend, JobStore
from aml_copilot.service.worker import create_worker_app, process_job

SENTINEL = "RAW-ROW-SENTINEL-do-not-leak"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
ACCOUNT = "ACC-H-00000"
INVESTIGATOR = {"Authorization": "Bearer dev-investigator"}
SUPERVISOR = {"Authorization": "Bearer dev-supervisor"}


def finished_state(account_id=ACCOUNT, status="draft_ready"):
    """What the orchestrator returns - including the raw `bundle` that must never be stored."""
    return {
        "account_id": account_id, "status": status, "status_detail": "A narrative draft is ready.",
        "steps_completed": ["evidence", "typology", "narrative", "faithfulness"],
        "bundle": EvidenceBundle(account_id=account_id, transactions=[{"transaction_id": "T1", "memo": SENTINEL}]),
        "narrative": SarNarrative(
            account_id=account_id, status="draft_for_human_review",
            introduction=[NarrativeStatement(text="Referred.")],
            facts=[NarrativeStatement(text="A fact.", evidence_ids=["T1"])],
            conclusion=[NarrativeStatement(text="The decision rests with the investigator.")],
        ),
    }


class Launcher:
    def __init__(self):
        self.queued, self.fail_with = [], None

    def enqueue(self, job_id):
        if self.fail_with:
            raise self.fail_with
        self.queued.append(job_id)


class World:
    """One API + one worker sharing a store and an audit log, with a controllable clock."""

    def __init__(self, run=None, daily_limit=30):
        self.now = NOW
        self.store = JobStore(InMemoryBackend())
        self.audit = AuditLog(InMemoryObjects())
        self.launcher = Launcher()
        self.runs = []
        self.run_fn = run or self._default_run
        self.ids = iter(f"job{n}" for n in range(1, 1000))
        self.client = TestClient(create_app(
            store=self.store, audit=self.audit, launcher=self.launcher,
            verifier=StaticTokenVerifier(dev_people()), clock=lambda: self.now,
            daily_limit=daily_limit, id_factory=lambda: next(self.ids),
        ))

    def _default_run(self, account_id):
        self.runs.append(account_id)
        return finished_state(account_id)

    def start(self, account=ACCOUNT, headers=INVESTIGATOR):
        return self.client.post("/investigations", json={"account_id": account}, headers=headers)

    def work(self, job_id):
        return process_job(job_id, store=self.store, audit=self.audit, run=self.run_fn, clock=lambda: self.now)

    def finished_job(self, account=ACCOUNT):
        job_id = self.start(account).json()["job_id"]
        self.work(job_id)
        return job_id, self.client.get(f"/investigations/{job_id}", headers=INVESTIGATOR).json()


# ---------------------------------------------------------------------------------- sign-in
def test_health_needs_no_sign_in():
    assert World().client.get("/status").json()["status"] == "ok"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic abc"}])
def test_every_other_route_refuses_a_missing_or_bad_token(headers):
    w = World()
    assert w.client.post("/investigations", json={"account_id": ACCOUNT}, headers=headers).status_code == 401
    assert w.client.get("/investigations", headers=headers).status_code == 401
    assert w.client.get("/investigations/x", headers=headers).status_code == 401
    assert w.client.post("/investigations/x/decision", headers=headers, json={}).status_code == 401
    assert w.client.get("/investigations/x/audit", headers=headers).status_code == 401
    assert w.launcher.queued == []


# ---------------------------------------------------------------------------------- starting
def test_a_valid_request_is_accepted_queued_and_audited():
    w = World()
    response = w.start()
    assert response.status_code == 202 and response.json() == {"job_id": "job1", "duplicate": False}
    assert w.launcher.queued == ["job1"] and w.store.get("job1").status == "QUEUED"
    first = w.audit.events("job1")[0]
    assert first.event_type == "JOB_SUBMITTED" and first.actor == "dev-investigator"


@pytest.mark.parametrize("bad", ["ACC-X-00001", "", "ACC-S-00001' OR '1'='1", "ACC-S-00001\nACC-S-00002"])
def test_a_bad_account_id_is_rejected_before_anything_is_written(bad):
    w = World()
    assert w.start(bad).status_code == 422
    assert w.launcher.queued == [] and w.store.list_recent() == []


def test_asking_again_while_one_is_open_returns_the_open_job_and_queues_nothing_new():
    w = World()
    w.start()
    again = w.start()
    assert again.status_code == 200 and again.json() == {"job_id": "job1", "duplicate": True}
    assert w.launcher.queued == ["job1"]


def test_past_the_daily_limit_the_answer_is_429_and_nothing_is_queued():
    w = World(daily_limit=1)
    assert w.start("ACC-H-00000").status_code == 202
    assert w.start("ACC-H-00001").status_code == 429
    assert w.launcher.queued == ["job1"]


def test_if_the_job_cannot_be_queued_it_is_marked_failed_and_the_account_is_free_again():
    w = World()
    w.launcher.fail_with = RuntimeError("queue down")
    assert w.start().status_code == 503
    job = w.store.get("job1")
    assert job.status == "FAILED_TO_START"
    assert [e.event_type for e in w.audit.events("job1")] == ["JOB_SUBMITTED", "RUN_FAILED"]
    w.launcher.fail_with = None
    assert w.start().status_code == 202  # not blocked by the earlier attempt


# ------------------------------------------------------------------------------- reading
def test_a_job_can_be_read_back_and_an_unknown_one_is_404():
    w = World()
    w.start()
    assert w.client.get("/investigations/job1", headers=INVESTIGATOR).json()["status"] == "QUEUED"
    assert w.client.get("/investigations/nope", headers=INVESTIGATOR).status_code == 404


def test_recent_jobs_are_listed():
    w = World()
    w.start()
    assert [j["job_id"] for j in w.client.get("/investigations", headers=INVESTIGATOR).json()] == ["job1"]


def test_the_raw_database_rows_never_reach_the_api_response_or_the_stored_job():
    w = World()
    job_id, body = w.finished_job()
    assert body["status"] == "COMPLETED" and body["review_status"] == "PENDING_REVIEW"
    assert SENTINEL not in w.client.get(f"/investigations/{job_id}", headers=INVESTIGATOR).text
    assert SENTINEL not in w.store.get(job_id).model_dump_json()
    assert SENTINEL not in w.client.get(f"/investigations/{job_id}/audit", headers=INVESTIGATOR).text


# ------------------------------------------------------------------------------ deciding
def decision(result_hash, kind="approve", comment="Reviewed the cited transactions."):
    return {"decision": kind, "comment": comment, "result_hash": result_hash}


def test_a_supervisor_can_approve_a_finished_draft():
    w = World()
    job_id, body = w.finished_job()
    response = w.client.post(f"/investigations/{job_id}/decision", headers=SUPERVISOR,
                             json=decision(body["result"]["result_hash"]))
    assert response.status_code == 200 and response.json()["review_status"] == "APPROVED"
    last = w.audit.events(job_id)[-1]
    assert last.event_type == "DECISION" and last.actor == "dev-supervisor" and last.details["decision"] == "approve"


def test_an_investigator_cannot_approve_but_can_reject_or_escalate():
    w = World()
    job_id, body = w.finished_job()
    h = body["result"]["result_hash"]
    refused = w.client.post(f"/investigations/{job_id}/decision", headers=INVESTIGATOR, json=decision(h))
    assert refused.status_code == 403 and w.store.get(job_id).review_status == "PENDING_REVIEW"
    escalated = w.client.post(f"/investigations/{job_id}/decision", headers=INVESTIGATOR, json=decision(h, "escalate"))
    assert escalated.status_code == 200 and escalated.json()["review_status"] == "ESCALATED"


def test_a_second_decision_is_409():
    w = World()
    job_id, body = w.finished_job()
    h = body["result"]["result_hash"]
    assert w.client.post(f"/investigations/{job_id}/decision", headers=SUPERVISOR, json=decision(h)).status_code == 200
    again = w.client.post(f"/investigations/{job_id}/decision", headers=SUPERVISOR, json=decision(h, "reject"))
    assert again.status_code == 409


def test_a_decision_naming_a_different_draft_is_409_and_not_audited():
    w = World()
    job_id, _ = w.finished_job()
    events_before = len(w.audit.events(job_id))
    response = w.client.post(f"/investigations/{job_id}/decision", headers=SUPERVISOR, json=decision("e" * 64))
    assert response.status_code == 409 and len(w.audit.events(job_id)) == events_before


def test_a_decision_on_an_unfinished_job_is_409():
    w = World()
    job_id = w.start().json()["job_id"]
    response = w.client.post(f"/investigations/{job_id}/decision", headers=SUPERVISOR, json=decision("a" * 64))
    assert response.status_code == 409


def test_a_decision_on_an_unknown_job_is_404_and_a_malformed_one_is_422():
    w = World()
    assert w.client.post("/investigations/nope/decision", headers=SUPERVISOR, json=decision("a" * 64)).status_code == 404
    assert w.client.post("/investigations/nope/decision", headers=SUPERVISOR,
                         json=decision("a" * 64, comment="  ")).status_code == 422


# ---------------------------------------------------------------------------------- audit
def test_the_whole_story_is_in_the_audit_trail_and_its_chain_verifies():
    w = World()
    job_id, body = w.finished_job()
    w.client.post(f"/investigations/{job_id}/decision", headers=SUPERVISOR, json=decision(body["result"]["result_hash"]))
    trail = w.client.get(f"/investigations/{job_id}/audit", headers=INVESTIGATOR).json()
    assert [e["event_type"] for e in trail["events"]] == ["JOB_SUBMITTED", "RUN_STARTED", "RUN_FINISHED", "DECISION"]
    assert trail["chain_ok"] is True and trail["chain_problem"] is None
    finished = trail["events"][2]["details"]
    assert finished["result_hash"] == body["result"]["result_hash"]


def test_a_tampered_trail_is_reported_by_the_api():
    w = World()
    job_id, _ = w.finished_job()
    objects = w.audit._objects
    name = "audit/job1/000002.json"
    objects._objects[name] = objects._objects[name].replace("RUN_STARTED", "RUN_FAILED")
    trail = w.client.get(f"/investigations/{job_id}/audit", headers=INVESTIGATOR).json()
    assert trail["chain_ok"] is False and "changed" in trail["chain_problem"]


# ---------------------------------------------------------------------------------- worker
def test_a_duplicate_delivery_runs_the_pipeline_once():
    w = World()
    job_id = w.start().json()["job_id"]
    assert [w.work(job_id), w.work(job_id), w.work(job_id)] == ["ran", "skipped", "skipped"]
    assert w.runs == [ACCOUNT]
    assert [e.event_type for e in w.audit.events(job_id)] == ["JOB_SUBMITTED", "RUN_STARTED", "RUN_FINISHED"]


def test_a_crash_in_the_pipeline_fails_the_job_instead_of_raising():
    def explode(account_id):
        raise RuntimeError("model unreachable")

    w = World(run=explode)
    job_id = w.start().json()["job_id"]
    assert w.work(job_id) == "failed"
    job = w.store.get(job_id)
    assert job.status == "FAILED" and "model unreachable" in job.error and job.review_status == "NOT_REQUIRED"
    assert w.audit.events(job_id)[-1].event_type == "RUN_FAILED"
    assert w.start().status_code == 202  # the account is free to try again


def test_the_pipeline_reporting_failure_in_its_state_is_a_finished_job_not_a_crash():
    def failed(account_id):
        return {"account_id": account_id, "status": "failed", "status_detail": "The evidence step failed: boom",
                "error": "boom", "failed_step": "evidence"}

    w = World(run=failed)
    job_id = w.start().json()["job_id"]
    assert w.work(job_id) == "ran"
    job = w.store.get(job_id)
    assert job.status == "COMPLETED" and job.result.status == "failed" and job.review_status == "NOT_REQUIRED"


def test_a_finished_run_records_how_long_it_took_in_its_provenance():
    w = World()
    _, body = w.finished_job()
    assert "total" in body["result"]["provenance"]["step_seconds"]


def test_the_worker_endpoint_runs_a_job_for_a_verified_caller_and_refuses_others():
    w = World()
    job_id = w.start().json()["job_id"]

    class Refuse:
        def verify(self, authorization):
            raise AuthError("nope")

    def worker(verifier):
        return TestClient(create_worker_app(store=w.store, audit=w.audit, run=w.run_fn, verifier=verifier,
                                            clock=lambda: w.now))

    assert worker(Refuse()).post("/internal/run", json={"job_id": job_id}).status_code == 401
    assert w.store.get(job_id).status == "QUEUED"
    ok = worker(AllowAnyCaller()).post("/internal/run", json={"job_id": job_id})
    assert ok.json() == {"job_id": job_id, "outcome": "ran"}
    assert worker(AllowAnyCaller()).post("/internal/run", json={"job_id": job_id}).json()["outcome"] == "skipped"


def test_the_clock_is_used_for_timestamps():
    w = World()
    w.now = NOW + timedelta(hours=3)
    w.start()
    assert w.store.get("job1").created_at == w.now.isoformat()


def test_step_timings_reported_by_the_pipeline_are_kept_in_the_provenance():
    w = World(run=lambda account_id: {**finished_state(account_id), "step_seconds": {"evidence": 4.2, "narrative": 9.1}})
    _, body = w.finished_job()
    seconds = body["result"]["provenance"]["step_seconds"]
    assert seconds["evidence"] == 4.2 and seconds["narrative"] == 9.1 and "total" in seconds
