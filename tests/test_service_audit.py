"""The audit chain: append-only, ordered, and any later edit, deletion or reorder is detected."""

import json

import pytest

from aml_copilot.service.audit import (
    GENESIS_HASH,
    AuditChainError,
    AuditLog,
    InMemoryObjects,
    ObjectExistsError,
    compute_event_hash,
)

AT = "2026-10-11T12:00:00+00:00"


def log_with_events(count=3, job_id="j1"):
    objects = InMemoryObjects()
    log = AuditLog(objects)
    kinds = ["JOB_SUBMITTED", "RUN_STARTED", "RUN_FINISHED", "DECISION"]
    for n in range(count):
        log.append(job_id, kinds[n], at=AT, actor="user-1", details={"n": n})
    return log, objects


def test_events_are_numbered_from_one_and_start_the_chain_at_genesis():
    log, _ = log_with_events(2)
    first, second = log.events("j1")
    assert (first.seq, second.seq) == (1, 2)
    assert first.prev_hash == GENESIS_HASH and second.prev_hash == first.event_hash


def test_an_untouched_chain_verifies():
    log, _ = log_with_events(4)
    report = log.verify_chain("j1")
    assert report.ok and report.events == 4 and report.problem is None


def test_an_empty_trail_verifies_with_zero_events():
    report = AuditLog(InMemoryObjects()).verify_chain("nothing")
    assert report.ok and report.events == 0


def test_different_jobs_have_separate_chains():
    objects = InMemoryObjects()
    log = AuditLog(objects)
    log.append("a", "JOB_SUBMITTED", at=AT, actor="u")
    log.append("b", "JOB_SUBMITTED", at=AT, actor="u")
    assert [e.seq for e in log.events("a")] == [1] and [e.seq for e in log.events("b")] == [1]


def test_editing_an_old_event_is_detected():
    log, objects = log_with_events(3)
    name = "audit/j1/000002.json"
    data = json.loads(objects.read(name))
    data["details"] = {"n": "changed"}
    objects._objects[name] = json.dumps(data)  # an attacker rewriting storage directly
    report = log.verify_chain("j1")
    assert not report.ok and "event 2 was changed" in report.problem


def test_rewriting_an_event_and_its_hash_breaks_the_link_to_the_next_one():
    log, objects = log_with_events(3)
    name = "audit/j1/000002.json"
    event = log.events("j1")[1]
    forged = event.model_copy(update={"details": {"n": "forged"}})
    forged = forged.model_copy(update={"event_hash": compute_event_hash(forged)})
    objects._objects[name] = forged.model_dump_json()
    report = log.verify_chain("j1")
    assert not report.ok and "event 3 does not follow" in report.problem


def test_deleting_a_middle_event_is_detected():
    log, objects = log_with_events(3)
    del objects._objects["audit/j1/000002.json"]
    report = log.verify_chain("j1")
    assert not report.ok and "missing or moved" in report.problem


def test_an_existing_event_cannot_be_overwritten():
    objects = InMemoryObjects()
    objects.put_if_absent("audit/j1/000001.json", "first")
    with pytest.raises(ObjectExistsError):
        objects.put_if_absent("audit/j1/000001.json", "second")
    assert objects.read("audit/j1/000001.json") == "first"


def test_a_writer_that_loses_the_race_takes_the_next_number():
    objects = InMemoryObjects()
    log = AuditLog(objects)
    log.append("j1", "JOB_SUBMITTED", at=AT, actor="u")
    original = objects.put_if_absent
    stolen = []

    def put_after_a_rival_wrote_seq_2(name, data):
        if not stolen:  # a rival writer slips in event 2 between our read and our write
            stolen.append(True)
            rival_event = log.events("j1")[0]
            original("audit/j1/000002.json", json.dumps({**json.loads(rival_event.model_dump_json()), "seq": 2}))
        original(name, data)

    objects.put_if_absent = put_after_a_rival_wrote_seq_2
    event = log.append("j1", "RUN_STARTED", at=AT, actor="u")
    assert event.seq == 3 and len(log.events("j1")) == 3


def test_giving_up_after_repeated_losses_raises_rather_than_overwriting():
    class AlwaysTaken(InMemoryObjects):
        def put_if_absent(self, name, data):
            raise ObjectExistsError(name)

    with pytest.raises(AuditChainError):
        AuditLog(AlwaysTaken()).append("j1", "JOB_SUBMITTED", at=AT, actor="u")
