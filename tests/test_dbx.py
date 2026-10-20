"""The Databricks entry points: investigate, review, secrets and the job mains - all on fakes."""

import os
from datetime import UTC, datetime

import pytest

from aml_copilot.agents.models import NarrativeStatement, SarNarrative
from aml_copilot.config import Settings
from aml_copilot.dbx import jobs
from aml_copilot.dbx.investigate import investigate
from aml_copilot.dbx.review import ReviewError, run_action
from aml_copilot.dbx.runtime import apply_config, load_secrets
from aml_copilot.errors import ConfigError
from aml_copilot.service.audit import AuditLog, InMemoryObjects
from aml_copilot.service.auth import ForbiddenError
from aml_copilot.service.jobs import (
    DecisionConflictError,
    InMemoryBackend,
    JobNotFoundError,
    JobStore,
    new_job,
)
from tests.test_service_delta import FakeDelta

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ACCOUNT = "ACC-H-00000"


@pytest.fixture(autouse=True)
def private_environment(monkeypatch):
    """The job mains copy secrets and settings into os.environ; give each test its own copy so nothing leaks."""
    monkeypatch.setattr(os, "environ", os.environ.copy())


def finished_state(account_id=ACCOUNT, status="draft_ready"):
    return {
        "account_id": account_id, "status": status, "status_detail": "A narrative draft is ready.",
        "steps_completed": ["evidence", "typology", "narrative"],
        "narrative": SarNarrative(
            account_id=account_id, status="draft_for_human_review",
            introduction=[NarrativeStatement(text="Referred.")],
            facts=[NarrativeStatement(text="A fact.", evidence_ids=["T1"])],
            conclusion=[NarrativeStatement(text="The decision rests with the investigator.")],
        ),
    }


def services():
    return JobStore(InMemoryBackend()), AuditLog(InMemoryObjects())


def run_one(store, audit, account=ACCOUNT, run=None, limit=30):
    return investigate(account, "owner@example.com", store=store, audit=audit, run=run or (lambda a: finished_state(a)),
                       clock=lambda: NOW, daily_limit=limit, new_id=iter(f"job{n}" for n in range(99)).__next__)


# ------------------------------------------------------------------------------- investigate
def test_an_investigation_runs_stores_the_draft_and_writes_the_audit_trail():
    store, audit = services()
    report = run_one(store, audit)
    assert report.ok and report.outcome == "ran" and report.job_id == "job0"
    job = store.get("job0")
    assert job.status == "COMPLETED" and job.review_status == "PENDING_REVIEW" and job.created_by == "owner@example.com"
    assert [e.event_type for e in audit.events("job0")] == ["JOB_SUBMITTED", "RUN_STARTED", "RUN_FINISHED"]
    assert audit.verify_chain("job0").ok


def test_an_open_investigation_of_the_same_account_is_reported_as_a_duplicate_not_an_error():
    store, audit = services()
    store.submit(new_job("open", ACCOUNT, created_by="x", now=NOW), now=NOW, daily_limit=5)
    report = run_one(store, audit)
    assert report.outcome == "duplicate" and report.job_id == "open" and report.ok


def test_past_the_daily_limit_the_job_fails_visibly():
    store, audit = services()
    report = run_one(store, audit, limit=0)
    assert report.outcome == "quota_exceeded" and not report.ok


def test_a_crashing_pipeline_is_recorded_and_reported_as_failed():
    store, audit = services()

    def explode(account_id):
        raise RuntimeError("model unreachable")

    report = run_one(store, audit, run=explode)
    assert report.outcome == "failed" and "model unreachable" in report.detail and not report.ok
    assert audit.events("job0")[-1].event_type == "RUN_FAILED"


def test_a_bad_account_id_is_rejected_before_anything_is_written():
    store, audit = services()
    with pytest.raises(ValueError):
        run_one(store, audit, account="ACC-X-1' OR '1'='1")
    assert store.list_recent() == []


# ---------------------------------------------------------------------------------- review
ROLES = {"boss@example.com": "supervisor", "analyst@example.com": "investigator"}


def act(action, store, audit, user="boss@example.com", job_id="job0", comment="Reviewed the cited transactions."):
    return run_action(action, job_id, comment, user, NOW, store=store, audit=audit, roles=ROLES.get)


def finished():
    store, audit = services()
    run_one(store, audit)
    return store, audit


def test_showing_an_investigation_includes_the_draft_and_a_verified_audit_trail():
    store, audit = finished()
    text = act("show", store, audit)
    assert "SAR NARRATIVE DRAFT" in text and "Audit trail verified" in text and "JOB_SUBMITTED" in text


def test_listing_shows_recent_investigations():
    store, audit = finished()
    assert "job0" in act("list", store, audit) and ACCOUNT in act("list", store, audit)


def test_a_supervisor_can_approve_once_and_it_is_audited():
    store, audit = finished()
    assert "APPROVED" in act("approve", store, audit)
    assert audit.events("job0")[-1].event_type == "DECISION" and audit.verify_chain("job0").ok
    with pytest.raises(DecisionConflictError):
        act("reject", store, audit)


def test_an_investigator_may_reject_or_escalate_but_not_approve():
    store, audit = finished()
    with pytest.raises(ForbiddenError, match="supervisor"):
        act("approve", store, audit, user="analyst@example.com")
    assert store.get("job0").review_status == "PENDING_REVIEW"
    assert "ESCALATED" in act("escalate", store, audit, user="analyst@example.com")


def test_someone_not_in_the_reviewers_table_cannot_decide():
    store, audit = finished()
    with pytest.raises(ForbiddenError, match="reviewers"):
        act("approve", store, audit, user="stranger@example.com")


def test_a_decision_needs_a_real_comment_and_a_known_investigation():
    store, audit = finished()
    with pytest.raises(ValueError):
        act("approve", store, audit, comment="   ")
    with pytest.raises(JobNotFoundError):
        act("approve", store, audit, job_id="nope")


def test_an_unknown_action_is_refused():
    store, audit = finished()
    with pytest.raises(ReviewError):
        act("delete", store, audit)


# ---------------------------------------------------------------------------------- runtime
class FakeDbutils:
    def __init__(self, secrets):
        self._secrets = secrets
        self.secrets = self

    def get(self, scope, key):
        if key not in self._secrets:
            raise RuntimeError(f"no secret {key}")
        return self._secrets[key]


def test_secrets_are_copied_into_the_environment_and_only_names_are_returned():
    env = {}
    loaded = load_secrets(FakeDbutils({"SF_SECURITY_TOKEN": "sk-secret", "SF_PASSWORD": "pw"}), "scope", environ=env)
    assert sorted(loaded) == ["SF_PASSWORD", "SF_SECURITY_TOKEN"]
    assert env["SF_SECURITY_TOKEN"] == "sk-secret" and "sk-secret" not in str(loaded)


def test_settings_are_applied_as_upper_case_environment_variables():
    env = {}
    assert apply_config(["llm_provider=anthropic", "AML_CHROMA_DIR=/tmp/x=y"], env) == ["LLM_PROVIDER", "AML_CHROMA_DIR"]
    assert env == {"LLM_PROVIDER": "anthropic", "AML_CHROMA_DIR": "/tmp/x=y"}
    with pytest.raises(ConfigError):
        apply_config(["no-equals-sign"], env)


# ---------------------------------------------------------------------------------- job mains
class FakeSpark:
    """Looks like a Spark session to `spark_sql`: .sql(statement, args=...).collect() -> rows with asDict()."""

    def __init__(self, delta):
        self.delta = delta

    def sql(self, statement, args=None):
        rows = self.delta(statement, args or {})

        class Row:
            def __init__(self, data):
                self.data = data

            def asDict(self):
                return self.data

        class Frame:
            def collect(self_inner):
                return [Row(r) for r in rows]

        return Frame()


def patch_pipeline(monkeypatch, run=None):
    monkeypatch.setattr("aml_copilot.retrieval.store.build_index", lambda *a, **k: 229)
    monkeypatch.setattr("aml_copilot.service.pipeline.make_run_fn", lambda settings: run or (lambda a: finished_state(a)))


def test_the_investigate_job_runs_end_to_end_on_the_delta_fake(monkeypatch, capsys):
    patch_pipeline(monkeypatch)
    delta = FakeDelta()
    code = jobs.investigate_main(["--account-id", ACCOUNT, "--set", "LLM_PROVIDER=gemini"],
                                 spark=FakeSpark(delta), dbutils=FakeDbutils({"SF_SECURITY_TOKEN": "k"}))
    out = capsys.readouterr().out
    assert code == 0 and "RESULT ran" in out and "SF_SECURITY_TOKEN" in out and "k\n" not in out
    assert len(delta.docs) >= 3 and len(delta.objects) == 3  # job + quota + lock documents; three audit events


def test_the_investigate_job_exits_non_zero_when_the_run_fails(monkeypatch, capsys):
    def explode(account_id):
        raise RuntimeError("boom")

    patch_pipeline(monkeypatch, run=explode)
    code = jobs.investigate_main(["--account-id", ACCOUNT], spark=FakeSpark(FakeDelta()), dbutils=FakeDbutils({}))
    assert code == 1 and "RESULT failed" in capsys.readouterr().out


def test_the_job_refuses_a_hostile_account_id(monkeypatch):
    patch_pipeline(monkeypatch)
    with pytest.raises(ValueError):
        jobs.investigate_main(["--account-id", "ACC-S-1; DROP TABLE x"], spark=FakeSpark(FakeDelta()), dbutils=FakeDbutils({}))




def test_provenance_names_the_model_for_each_step():
    from aml_copilot.service.pipeline import make_provenance

    p = make_provenance(Settings(_env_file=None, anthropic_model="claude-haiku-4-5", narrative_model="claude-sonnet-5",
                                 llm_provider="vertex", build_sha="abc123"))
    assert p.models == {"evidence": "claude-haiku-4-5", "typology": "claude-haiku-4-5",
                        "narrative": "claude-sonnet-5", "faithfulness": "claude-haiku-4-5"}
    assert p.llm_provider == "vertex" and p.build == {"sha": "abc123"}


# ------------------------------------------------------------------------------- choosing the model family
def test_each_run_records_which_model_family_wrote_it():
    store, audit = services()
    investigate(ACCOUNT, "o@example.com", store=store, audit=audit, run=lambda a: finished_state(a), clock=lambda: NOW,
                daily_limit=9, provider="claude", new_id=lambda: "jx")
    assert store.get("jx").provider == "claude"
    assert audit.events("jx")[0].details["provider"] == "claude"


def test_a_claude_run_switches_the_model_settings_and_a_gemini_run_leaves_them(monkeypatch, capsys):
    patch_pipeline(monkeypatch)
    seen = {}
    monkeypatch.setattr("aml_copilot.service.pipeline.make_provenance", lambda s: (seen.update(provider=s.llm_provider, judge=s.typology_model, summarizer=s.llm_model), None)[1])
    jobs.investigate_main(["--account-id", ACCOUNT, "--provider", "claude", "--set", "LLM_PROVIDER=gemini"],
                          spark=FakeSpark(FakeDelta()), dbutils=FakeDbutils({}))
    assert seen == {"provider": "anthropic", "judge": "claude-sonnet-5", "summarizer": "claude-haiku-4-5"}
    seen.clear()
    jobs.investigate_main(["--account-id", ACCOUNT, "--set", "LLM_PROVIDER=gemini", "--set", "LLM_MODEL=gemini-3.5-flash"],
                          spark=FakeSpark(FakeDelta()), dbutils=FakeDbutils({}))
    assert seen["provider"] == "gemini" and seen["summarizer"] == "gemini-3.5-flash"


def test_the_provider_must_be_known():
    from aml_copilot.dbx.providers import provider_settings

    assert provider_settings("gemini") == [] and "LLM_PROVIDER=anthropic" in provider_settings("claude")
    with pytest.raises(ConfigError):
        provider_settings("gpt")
    with pytest.raises(SystemExit):
        jobs.investigate_main(["--account-id", ACCOUNT, "--provider", "gpt"], spark=FakeSpark(FakeDelta()), dbutils=FakeDbutils({}))
