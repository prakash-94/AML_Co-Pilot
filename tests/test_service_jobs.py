"""Job rules: run once, decide once, daily quota, no duplicates - tested on the in-memory backend."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from aml_copilot.service.jobs import (
    DecisionConflictError,
    DecisionRecord,
    InMemoryBackend,
    JobNotFoundError,
    JobStore,
    StaleDraftError,
    new_job,
)
from aml_copilot.service.models import InvestigationResult, to_api_result

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
ACCOUNT = "ACC-H-00000"


def make_result(status="draft_ready", detail="ready") -> InvestigationResult:
    return to_api_result({"account_id": ACCOUNT, "status": status, "status_detail": detail})


def job(job_id="j1", account=ACCOUNT, now=NOW):
    return new_job(job_id, account, created_by="user-1", created_by_email="a@example.com", now=now)


def store():
    return JobStore(InMemoryBackend())


def submit(s, j=None, now=NOW, limit=30, minutes=10):
    return s.submit(j or job(), now=now, daily_limit=limit, dedupe_minutes=minutes)


def decision(result_hash, kind="approve", comment="Reviewed."):
    return DecisionRecord(decision=kind, comment=comment, result_hash=result_hash, decided_by="boss-1",
                          role="supervisor", decided_at=NOW.isoformat())


def finished_pending(s, job_id="j1"):
    """A submitted, claimed and completed job with a draft awaiting review; returns the stored result."""
    submit(s, job(job_id))
    s.claim(job_id, now=NOW)
    result = make_result()
    s.complete(job_id, ACCOUNT, result, now=NOW)
    return result


def run_in_threads(target, count):
    threads = [threading.Thread(target=target, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ------------------------------------------------------------------------------ submitting
def test_a_new_job_is_created_queued():
    s = store()
    outcome = submit(s)
    assert (outcome.kind, outcome.job_id) == ("created", "j1")
    saved = s.get("j1")
    assert saved.status == "QUEUED" and saved.review_status == "NOT_REQUIRED" and saved.created_by == "user-1"


def test_asking_again_while_one_is_open_returns_the_open_job_and_costs_no_quota():
    s = store()
    submit(s, job("j1"))
    again = submit(s, job("j2"), limit=1)  # would be over quota if it counted
    assert (again.kind, again.job_id) == ("duplicate", "j1")
    assert s.get("j2") is None


def test_the_same_account_can_be_run_again_after_the_dedupe_window():
    s = store()
    submit(s, job("j1"), minutes=10)
    later = NOW + timedelta(minutes=11)
    assert submit(s, job("j2", now=later), now=later).kind == "created"


def test_a_finished_job_releases_its_account_straight_away():
    s = store()
    finished_pending(s, "j1")
    assert submit(s, job("j2")).kind == "created"


def test_a_failed_job_also_releases_its_account():
    s = store()
    submit(s, job("j1"))
    s.fail("j1", ACCOUNT, "boom", now=NOW)
    assert submit(s, job("j2")).kind == "created"


def test_different_accounts_do_not_block_each_other():
    s = store()
    submit(s, job("j1", account="ACC-H-00000"))
    assert submit(s, job("j2", account="ACC-H-00001")).kind == "created"


def test_the_daily_quota_stops_new_jobs_and_resets_the_next_day():
    s = store()
    assert submit(s, job("j1", account="ACC-H-00000"), limit=2).kind == "created"
    assert submit(s, job("j2", account="ACC-H-00001"), limit=2).kind == "created"
    blocked = submit(s, job("j3", account="ACC-H-00002"), limit=2)
    assert (blocked.kind, blocked.job_id) == ("quota_exceeded", None) and s.get("j3") is None
    tomorrow = NOW + timedelta(days=1)
    assert submit(s, job("j4", account="ACC-H-00002", now=tomorrow), now=tomorrow, limit=2).kind == "created"


def test_many_simultaneous_requests_for_one_account_create_exactly_one_job():
    s = store()
    outcomes = []
    run_in_threads(lambda n: outcomes.append(submit(s, job(f"j{n}")).kind), 25)
    assert outcomes.count("created") == 1 and outcomes.count("duplicate") == 24


def test_simultaneous_requests_cannot_exceed_the_daily_quota():
    s = store()
    outcomes = []
    run_in_threads(lambda n: outcomes.append(submit(s, job(f"j{n}", account=f"ACC-H-{n:05d}"), limit=5).kind), 30)
    assert outcomes.count("created") == 5 and outcomes.count("quota_exceeded") == 25


# --------------------------------------------------------------------------------- claiming
def test_claim_moves_queued_to_running_once():
    s = store()
    submit(s)
    first = s.claim("j1", now=NOW)
    assert first.status == "RUNNING" and first.account_id == ACCOUNT and first.started_at == NOW.isoformat()
    assert s.claim("j1", now=NOW) is None  # a duplicate delivery gets nothing


def test_claiming_an_unknown_job_gives_nothing():
    assert store().claim("nope", now=NOW) is None


def test_many_simultaneous_claims_have_exactly_one_winner():
    s = store()
    submit(s)
    winners = []

    def go(_):
        got = s.claim("j1", now=NOW)
        if got is not None:
            winners.append(got)

    run_in_threads(go, 30)
    assert len(winners) == 1


# ------------------------------------------------------------------------------ finishing
def test_a_draft_ready_result_waits_for_review():
    s = store()
    result = finished_pending(s)
    done = s.get("j1")
    assert done.status == "COMPLETED" and done.review_status == "PENDING_REVIEW"
    assert done.result.result_hash == result.result_hash and done.finished_at == NOW.isoformat()


@pytest.mark.parametrize("status", ["no_action", "incomplete", "failed"])
def test_a_result_that_is_not_a_draft_needs_no_review(status):
    s = store()
    submit(s)
    s.claim("j1", now=NOW)
    s.complete("j1", ACCOUNT, make_result(status), now=NOW)
    assert s.get("j1").review_status == "NOT_REQUIRED"


def test_failing_records_a_short_error_and_the_status():
    s = store()
    submit(s)
    s.fail("j1", ACCOUNT, "x" * 5000, now=NOW, status="FAILED_TO_START")
    failed = s.get("j1")
    assert failed.status == "FAILED_TO_START" and len(failed.error) == 500 and failed.result is None


def test_finishing_an_unknown_job_is_an_error():
    with pytest.raises(JobNotFoundError):
        store().complete("nope", ACCOUNT, make_result(), now=NOW)


# ------------------------------------------------------------------------------- deciding
def test_a_reviewer_can_decide_a_finished_draft_once():
    s = store()
    result = finished_pending(s)
    decided = s.decide("j1", decision(result.result_hash, "approve"))
    assert decided.review_status == "APPROVED" and decided.decision.decided_by == "boss-1"
    assert s.get("j1").decision.comment == "Reviewed."


@pytest.mark.parametrize(("kind", "review"), [("approve", "APPROVED"), ("reject", "REJECTED"), ("escalate", "ESCALATED")])
def test_each_decision_maps_to_its_review_status(kind, review):
    s = store()
    result = finished_pending(s)
    assert s.decide("j1", decision(result.result_hash, kind)).review_status == review


def test_a_second_decision_is_refused():
    s = store()
    result = finished_pending(s)
    s.decide("j1", decision(result.result_hash, "approve"))
    with pytest.raises(DecisionConflictError):
        s.decide("j1", decision(result.result_hash, "reject"))
    assert s.get("j1").review_status == "APPROVED"  # unchanged


def test_a_decision_on_a_job_that_is_still_running_is_refused():
    s = store()
    submit(s)
    s.claim("j1", now=NOW)
    with pytest.raises(DecisionConflictError):
        s.decide("j1", decision("a" * 64))


def test_a_decision_that_names_a_different_draft_is_refused_and_changes_nothing():
    s = store()
    finished_pending(s)
    with pytest.raises(StaleDraftError):
        s.decide("j1", decision("f" * 64))
    assert s.get("j1").review_status == "PENDING_REVIEW" and s.get("j1").decision is None


def test_deciding_an_unknown_job_is_an_error():
    with pytest.raises(JobNotFoundError):
        store().decide("nope", decision("a" * 64))


def test_simultaneous_decisions_have_exactly_one_winner():
    s = store()
    result = finished_pending(s)
    winners, losers = [], []
    kinds = ["approve", "reject", "escalate"] * 8

    def go(n):
        try:
            s.decide("j1", decision(result.result_hash, kinds[n]))
            winners.append(kinds[n])
        except DecisionConflictError:
            losers.append(kinds[n])

    run_in_threads(go, len(kinds))
    assert len(winners) == 1 and len(losers) == 23


# ------------------------------------------------------------------------------- listing
def test_recent_jobs_come_newest_first_and_respect_the_limit():
    s = store()
    for n in range(5):
        when = NOW + timedelta(minutes=n)
        submit(s, job(f"j{n}", account=f"ACC-H-{n:05d}", now=when), now=when)
    assert [j.job_id for j in s.list_recent(3)] == ["j4", "j3", "j2"]
