"""The web page's server: who may do what, the buttons that change data, and the Jobs API launcher (all on fakes)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from aml_copilot.agents.models import NarrativeStatement, SarNarrative
from aml_copilot.dbx.jobs_api import JobsApiLauncher
from aml_copilot.dbx.webapp import create_app
from aml_copilot.errors import SourceUnavailableError
from aml_copilot.service.audit import AuditLog, InMemoryObjects
from aml_copilot.service.jobs import InMemoryBackend, JobStore, new_job
from aml_copilot.service.models import to_api_result

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
ACCOUNT = "ACC-H-00000"
ROLES = {"boss@example.com": "supervisor", "analyst@example.com": "investigator"}
BOSS = {"X-Forwarded-Email": "boss@example.com"}
ANALYST = {"X-Forwarded-Email": "analyst@example.com"}
STRANGER = {"X-Forwarded-Email": "stranger@example.com"}
SAME_ORIGIN = {"X-Requested-With": "aml-copilot"}
SENTINEL = "RAW-ROW-SENTINEL"


class FakeLauncher:
    def __init__(self):
        self.started, self.fail = [], False

    def start(self, account_id, requested_by):
        if self.fail:
            raise SourceUnavailableError("databricks jobs", "boom")
        self.started.append((account_id, requested_by))
        return {"run_id": 4242}

    def status(self, run_id):
        return {"state": "TERMINATED", "result": "SUCCESS", "message": ""}


class World:
    def __init__(self, tmp_path):
        (tmp_path / "index.html").write_text("<title>page</title>", encoding="utf-8")
        self.store, self.audit, self.launcher = JobStore(InMemoryBackend()), AuditLog(InMemoryObjects()), FakeLauncher()
        self.client = TestClient(create_app(store=self.store, audit=self.audit, roles=ROLES.get, launcher=self.launcher,
                                            clock=lambda: NOW, static_dir=tmp_path))

    def finished_job(self, job_id="j1"):
        self.store.submit(new_job(job_id, ACCOUNT, created_by="boss@example.com", now=NOW), now=NOW, daily_limit=9)
        self.store.claim(job_id, now=NOW)
        narrative = SarNarrative(account_id=ACCOUNT, status="draft_for_human_review",
                                 introduction=[NarrativeStatement(text="Referred.")],
                                 facts=[NarrativeStatement(text="A fact.", evidence_ids=["T1"])],
                                 conclusion=[NarrativeStatement(text="The decision rests with the investigator.")])
        result = to_api_result({"account_id": ACCOUNT, "status": "draft_ready", "status_detail": "ready",
                                "narrative": narrative, "bundle": {"memo": SENTINEL}})
        self.store.complete(job_id, ACCOUNT, result, now=NOW)
        self.audit.append(job_id, "JOB_SUBMITTED", at=NOW.isoformat(), actor="boss@example.com")
        return result


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def decision(result_hash, kind="approve", comment="Reviewed."):
    return {"decision": kind, "comment": comment, "result_hash": result_hash}


# ------------------------------------------------------------------------------------ who you are
def test_a_request_with_no_identity_is_401(world):
    assert world.client.get("/api/me").status_code == 401


def test_someone_not_in_the_reviewers_table_is_403_everywhere(world):
    world.finished_job()
    for path in ("/api/me", "/api/investigations", "/api/investigations/j1", "/api/investigations/j1/audit", "/api/runs/1"):
        assert world.client.get(path, headers=STRANGER).status_code == 403
    assert world.client.post("/api/investigations", headers={**STRANGER, **SAME_ORIGIN}, json={"account_id": ACCOUNT}).status_code == 403


def test_a_listed_person_sees_their_email_and_role(world):
    assert world.client.get("/api/me", headers=BOSS).json() == {"email": "boss@example.com", "role": "supervisor"}
    assert world.client.get("/api/me", headers=ANALYST).json()["role"] == "investigator"


def test_a_development_user_can_stand_in_locally(tmp_path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    app = create_app(store=JobStore(InMemoryBackend()), audit=AuditLog(InMemoryObjects()), roles=ROLES.get,
                     launcher=FakeLauncher(), clock=lambda: NOW, dev_user="boss@example.com", static_dir=tmp_path)
    assert TestClient(app).get("/api/me").json()["email"] == "boss@example.com"


# ------------------------------------------------------------------------------------- reading
def test_the_list_summarises_each_investigation(world):
    world.finished_job()
    [item] = world.client.get("/api/investigations", headers=BOSS).json()
    assert item["account_id"] == ACCOUNT and item["outcome"] == "draft_ready" and item["review_status"] == "PENDING_REVIEW"


def test_an_investigation_and_its_audit_trail_can_be_read_and_raw_rows_never_appear(world):
    world.finished_job()
    body = world.client.get("/api/investigations/j1", headers=BOSS)
    assert body.json()["result"]["status"] == "draft_ready" and SENTINEL not in body.text
    trail = world.client.get("/api/investigations/j1/audit", headers=BOSS).json()
    assert trail["chain_ok"] is True and trail["events"][0]["event_type"] == "JOB_SUBMITTED"
    assert world.client.get("/api/investigations/nope", headers=BOSS).status_code == 404


# ----------------------------------------------------------------------------------- starting
def test_starting_asks_databricks_to_run_the_job_as_the_signed_in_person(world):
    response = world.client.post("/api/investigations", headers={**ANALYST, **SAME_ORIGIN}, json={"account_id": " acc-h-00000 "})
    assert response.status_code == 202 and response.json() == {"run_id": 4242}
    assert world.launcher.started == [(ACCOUNT, "analyst@example.com")]


def test_a_forged_requester_in_the_body_is_ignored(world):
    world.client.post("/api/investigations", headers={**ANALYST, **SAME_ORIGIN}, json={"account_id": ACCOUNT, "requested_by": "boss@example.com"})
    assert world.launcher.started == [(ACCOUNT, "analyst@example.com")]


@pytest.mark.parametrize("bad", ["ACC-X-00001", "", "ACC-S-00001' OR '1'='1"])
def test_a_bad_account_id_is_422_and_nothing_is_started(world, bad):
    assert world.client.post("/api/investigations", headers={**BOSS, **SAME_ORIGIN}, json={"account_id": bad}).status_code == 422
    assert world.launcher.started == []


def test_buttons_that_change_data_refuse_calls_without_the_same_origin_header(world):
    world.finished_job()
    assert world.client.post("/api/investigations", headers=BOSS, json={"account_id": ACCOUNT}).status_code == 403
    assert world.client.post("/api/investigations/j1/decision", headers=BOSS, json=decision("a" * 64)).status_code == 403
    assert world.launcher.started == [] and world.store.get("j1").review_status == "PENDING_REVIEW"


def test_a_databricks_failure_is_a_502_not_a_crash(world):
    world.launcher.fail = True
    assert world.client.post("/api/investigations", headers={**BOSS, **SAME_ORIGIN}, json={"account_id": ACCOUNT}).status_code == 502


def test_a_run_can_be_polled(world):
    assert world.client.get("/api/runs/4242", headers=BOSS).json()["result"] == "SUCCESS"


# ----------------------------------------------------------------------------------- deciding
def test_a_supervisor_approves_once_and_it_is_audited(world):
    result = world.finished_job()
    ok = world.client.post("/api/investigations/j1/decision", headers={**BOSS, **SAME_ORIGIN}, json=decision(result.result_hash))
    assert ok.status_code == 200 and "APPROVED" in ok.json()["message"]
    assert world.audit.events("j1")[-1].event_type == "DECISION" and world.audit.verify_chain("j1").ok
    again = world.client.post("/api/investigations/j1/decision", headers={**BOSS, **SAME_ORIGIN}, json=decision(result.result_hash, "reject"))
    assert again.status_code == 409


def test_an_investigator_cannot_approve_but_can_escalate(world):
    result = world.finished_job()
    refused = world.client.post("/api/investigations/j1/decision", headers={**ANALYST, **SAME_ORIGIN}, json=decision(result.result_hash))
    assert refused.status_code == 403 and world.store.get("j1").review_status == "PENDING_REVIEW"
    escalated = world.client.post("/api/investigations/j1/decision", headers={**ANALYST, **SAME_ORIGIN}, json=decision(result.result_hash, "escalate"))
    assert escalated.status_code == 200 and world.store.get("j1").review_status == "ESCALATED"


def test_a_decision_naming_another_draft_is_409_and_changes_nothing(world):
    world.finished_job()
    response = world.client.post("/api/investigations/j1/decision", headers={**BOSS, **SAME_ORIGIN}, json=decision("f" * 64))
    assert response.status_code == 409 and world.store.get("j1").review_status == "PENDING_REVIEW"


def test_a_decision_without_a_comment_is_422(world):
    result = world.finished_job()
    assert world.client.post("/api/investigations/j1/decision", headers={**BOSS, **SAME_ORIGIN},
                             json=decision(result.result_hash, comment="  ")).status_code == 422


def test_deciding_on_an_unknown_investigation_is_404(world):
    assert world.client.post("/api/investigations/nope/decision", headers={**BOSS, **SAME_ORIGIN}, json=decision("a" * 64)).status_code == 404


# ----------------------------------------------------------------------------------------- page
def test_the_page_is_served_at_the_root(world):
    assert "<title>page</title>" in world.client.get("/").text


def test_the_page_is_never_cached_but_api_answers_are_left_alone(world):
    assert world.client.get("/").headers["cache-control"] == "no-cache"
    assert "cache-control" not in world.client.get("/api/me", headers=BOSS).headers


# ----------------------------------------------------------------------------- the Jobs API client
class FakeHttp:
    def __init__(self, payload=None, fail=False):
        self.calls, self.payload, self.fail = [], payload or {}, fail

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        http = self

        class Response:
            def raise_for_status(self):
                if http.fail:
                    raise RuntimeError("401 for token secret-token")

            def json(self):
                return http.payload

        return Response()


def test_the_launcher_runs_the_job_with_the_account_and_the_requester():
    http = FakeHttp({"run_id": 99})
    launcher = JobsApiLauncher("https://dbc-1.cloud.databricks.com/", lambda: "secret-token", 123, http=http)
    assert launcher.start(ACCOUNT, "boss@example.com") == {"run_id": 99}
    method, url, kwargs = http.calls[0]
    assert (method, url) == ("POST", "https://dbc-1.cloud.databricks.com/api/2.1/jobs/run-now")
    assert kwargs["json"] == {"job_id": 123, "job_parameters": {"account_id": ACCOUNT, "requested_by": "boss@example.com"}}
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


def test_the_launcher_reports_a_runs_state():
    http = FakeHttp({"state": {"life_cycle_state": "TERMINATED", "result_state": "FAILED", "state_message": "oops"}})
    status = JobsApiLauncher("dbc-1.cloud.databricks.com", lambda: "t", 1, http=http).status(7)
    assert status == {"state": "TERMINATED", "result": "FAILED", "message": "oops"}
    assert http.calls[0][2]["params"] == {"run_id": 7}


def test_a_launcher_error_never_contains_the_token():
    with pytest.raises(SourceUnavailableError) as caught:
        JobsApiLauncher("dbc-1.cloud.databricks.com", lambda: "secret-token", 1, http=FakeHttp(fail=True)).start(ACCOUNT, "x")
    assert "secret-token" not in str(caught.value)
