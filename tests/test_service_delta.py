"""The Delta adapters: the service rules run on a fake that follows Delta's MERGE / INSERT semantics."""

import json
import re
import threading
from datetime import UTC, datetime

import pytest

from aml_copilot.service.audit import AuditLog, ObjectExistsError
from aml_copilot.service.delta import (
    DeltaDocuments,
    DeltaObjects,
    PartialWriteError,
    TransactionConflictError,
    create_tables_sql,
)
from aml_copilot.service.jobs import (
    DecisionConflictError,
    DecisionRecord,
    JobStore,
    StaleDraftError,
    new_job,
)
from aml_copilot.service.models import to_api_result

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
SCHEMA = "workspace.aml_copilot"


class FakeDelta:
    """Just enough of Delta for the statements the adapters send, with the same row-level rules."""

    def __init__(self):
        self.docs = {}  # (collection, doc_id) -> [version, body]
        self.objects = {}  # name -> data
        self.statements = []
        self.lock = threading.RLock()
        self.before_merge = None  # hook: simulate another writer slipping in
        self.reviewers = {}  # email (lower case) -> role

    def __call__(self, statement, params):
        with self.lock:
            self.statements.append(statement)
            if statement.startswith("MERGE INTO"):
                if self.before_merge:
                    hook, self.before_merge = self.before_merge, None
                    hook(self)
                written = 0
                for i in range(len([k for k in params if k.startswith("wc")])):
                    key, expected, body = (params[f"wc{i}"], params[f"wd{i}"]), params[f"we{i}"], params[f"wb{i}"]
                    if key in self.docs and self.docs[key][0] == expected:
                        self.docs[key] = [expected + 1, body]
                        written += 1
                    elif key not in self.docs and expected == 0:
                        self.docs[key] = [1, body]
                        written += 1
                return [{"num_affected_rows": written}]
            if statement.startswith("CREATE TABLE"):
                return []
            if statement.startswith("SELECT current_user()"):
                return [{"u": "owner@example.com"}]
            if "FROM workspace.aml_copilot.reviewers" in statement:
                role = self.reviewers.get(params["e"].lower())
                return [{"role": role}] if role else []
            if "audit_objects" in statement:
                return self._objects(statement, params)
            if statement.startswith("SELECT collection, doc_id, version, body"):
                n = len([k for k in params if k.startswith("c")])
                keys = [(params[f"c{i}"], params[f"d{i}"]) for i in range(n)]
                return [{"collection": k[0], "doc_id": k[1], "version": self.docs[k][0], "body": self.docs[k][1]}
                        for k in keys if k in self.docs]
            if "ORDER BY get_json_object" in statement:
                field = re.search(r"\$\.(\w+)", statement).group(1)
                limit = int(re.search(r"LIMIT (\d+)", statement).group(1))
                docs = [json.loads(v[1]) for (c, _), v in self.docs.items() if c == params["c"]]
                docs.sort(key=lambda d: d.get(field, ""), reverse=True)
                return [{"body": json.dumps(d)} for d in docs[:limit]]
            if statement.startswith("SELECT body"):
                key = (params["c"], params["d"])
                return [{"body": self.docs[key][1]}] if key in self.docs else []
            raise AssertionError(f"unexpected statement: {statement}")

    def _objects(self, statement, params):
        if statement.startswith("INSERT"):
            if params["n"] in self.objects:
                return [{"num_affected_rows": 0}]
            self.objects[params["n"]] = params["d"]
            return [{"num_affected_rows": 1}]
        if "startswith(name" in statement:
            return [{"name": n} for n in sorted(self.objects) if n.startswith(params["p"])]
        if statement.startswith("SELECT data"):
            return [{"data": self.objects[params["n"]]}] if params["n"] in self.objects else []
        raise AssertionError(statement)


def store(fake=None):
    fake = fake or FakeDelta()
    return JobStore(DeltaDocuments(fake, SCHEMA)), fake


def job(job_id="j1", account="ACC-H-00000"):
    return new_job(job_id, account, created_by="u1", now=NOW)


# ------------------------------------------------------------------------------------ documents
def test_a_document_round_trips_and_an_unknown_one_is_none():
    db = DeltaDocuments(FakeDelta(), SCHEMA)
    db.transact([("c", "a")], lambda docs: {("c", "a"): {"x": 1}})
    assert db.get("c", "a") == {"x": 1} and db.get("c", "missing") is None


def test_each_write_bumps_the_version_and_reads_see_the_latest():
    fake = FakeDelta()
    db = DeltaDocuments(fake, SCHEMA)
    for n in range(3):
        db.transact([("c", "a")], lambda docs, n=n: {("c", "a"): {"n": n}})
    assert fake.docs[("c", "a")][0] == 3 and db.get("c", "a") == {"n": 2}


def test_a_transaction_writes_all_its_documents_in_one_statement():
    fake = FakeDelta()
    db = DeltaDocuments(fake, SCHEMA)
    db.transact([("c", "a"), ("c", "b")], lambda docs: {("c", "a"): {"v": 1}, ("c", "b"): {"v": 2}})
    assert sum(s.startswith("MERGE INTO") for s in fake.statements) == 1 and len(fake.docs) == 2


def test_if_the_rule_raises_nothing_is_written():
    fake = FakeDelta()
    db = DeltaDocuments(fake, SCHEMA)

    def refuse(docs):
        raise ValueError("no")

    with pytest.raises(ValueError):
        db.transact([("c", "a")], refuse)
    assert fake.docs == {}


def test_a_rule_that_writes_nothing_sends_no_merge():
    fake = FakeDelta()
    DeltaDocuments(fake, SCHEMA).transact([("c", "a")], lambda docs: {})
    assert not any(s.startswith("MERGE") for s in fake.statements)


def test_a_change_by_another_writer_is_detected_and_the_rule_is_retried_on_fresh_data():
    fake = FakeDelta()
    db = DeltaDocuments(fake, SCHEMA)
    db.transact([("c", "a")], lambda docs: {("c", "a"): {"n": 1}})
    seen = []

    def bump(docs):
        seen.append(docs[("c", "a")]["n"])
        return {("c", "a"): {"n": docs[("c", "a")]["n"] + 1}}

    fake.before_merge = lambda f: f.docs.__setitem__(("c", "a"), [2, json.dumps({"n": 10})])  # rival wrote first
    db.transact([("c", "a")], bump)
    assert seen == [1, 10] and db.get("c", "a") == {"n": 11}


def test_repeated_conflicts_give_up_without_overwriting():
    fake = FakeDelta()
    db = DeltaDocuments(fake, SCHEMA, attempts=2)
    db.transact([("c", "a")], lambda docs: {("c", "a"): {"n": 1}})

    def always_rival(f):
        f.docs[("c", "a")][0] += 1
        f.before_merge = always_rival

    fake.before_merge = always_rival
    with pytest.raises(TransactionConflictError):
        db.transact([("c", "a")], lambda docs: {("c", "a"): {"n": 99}})
    assert json.loads(fake.docs[("c", "a")][1]) == {"n": 1}


def test_a_partly_applied_transaction_is_reported_not_retried():
    fake = FakeDelta()
    db = DeltaDocuments(fake, SCHEMA)
    db.transact([("c", "a"), ("c", "b")], lambda docs: {("c", "a"): {"v": 1}, ("c", "b"): {"v": 1}})
    fake.before_merge = lambda f: f.docs[("c", "b")].__setitem__(0, 5)  # only b changes underneath
    with pytest.raises(PartialWriteError):
        db.transact([("c", "a"), ("c", "b")], lambda docs: {("c", "a"): {"v": 2}, ("c", "b"): {"v": 2}})


@pytest.mark.parametrize("bad", ["a b", "x; drop table y", "", "1abc"])
def test_a_field_name_that_is_not_plain_is_refused(bad):
    with pytest.raises(ValueError):
        DeltaDocuments(FakeDelta(), SCHEMA).list_recent("c", bad, 5)


def test_a_schema_that_is_not_plain_is_refused():
    with pytest.raises(ValueError):
        DeltaDocuments(FakeDelta(), "x; drop table y")


# ----------------------------------------------------------------------- the service rules on Delta
def test_the_job_rules_work_on_delta_end_to_end():
    s, _ = store()
    assert s.submit(job("j1"), now=NOW, daily_limit=5).kind == "created"
    assert s.submit(job("j2"), now=NOW, daily_limit=5).kind == "duplicate"  # same account is still open
    assert s.claim("j1", now=NOW).status == "RUNNING" and s.claim("j1", now=NOW) is None
    result = to_api_result({"account_id": "ACC-H-00000", "status": "draft_ready", "status_detail": "ready"})
    s.complete("j1", "ACC-H-00000", result, now=NOW)
    assert s.get("j1").review_status == "PENDING_REVIEW"
    record = DecisionRecord(decision="approve", comment="ok", result_hash=result.result_hash, decided_by="u",
                            role="supervisor", decided_at=NOW.isoformat())
    assert s.decide("j1", record).review_status == "APPROVED"
    with pytest.raises(DecisionConflictError):
        s.decide("j1", record)
    # the account is free again once the job finished, and the quota counts what was created
    assert s.submit(job("j3"), now=NOW, daily_limit=2).kind == "created"
    assert s.submit(job("j4", "ACC-H-00001"), now=NOW, daily_limit=2).kind == "quota_exceeded"


def test_a_decision_for_a_different_draft_is_refused_on_delta_too():
    s, _ = store()
    s.submit(job("j1"), now=NOW, daily_limit=5)
    s.claim("j1", now=NOW)
    s.complete("j1", "ACC-H-00000", to_api_result({"account_id": "ACC-H-00000", "status": "draft_ready",
                                                   "status_detail": "x"}), now=NOW)
    with pytest.raises(StaleDraftError):
        s.decide("j1", DecisionRecord(decision="approve", comment="ok", result_hash="f" * 64, decided_by="u",
                                      role="supervisor", decided_at=NOW.isoformat()))


def test_recent_jobs_are_listed_newest_first():
    s, _ = store()
    for n, hour in enumerate((9, 11, 10)):
        j = new_job(f"j{n}", f"ACC-H-0000{n}", created_by="u", now=NOW.replace(hour=hour))
        s.submit(j, now=NOW.replace(hour=hour), daily_limit=9)
    assert [j.job_id for j in s.list_recent(2)] == ["j1", "j2"]


# ------------------------------------------------------------------------------------ audit objects
def test_an_audit_object_can_be_written_once_and_listed_by_prefix():
    objects = DeltaObjects(FakeDelta(), SCHEMA)
    objects.put_if_absent("audit/j1/000001.json", "a")
    objects.put_if_absent("audit/j1/000002.json", "b")
    objects.put_if_absent("audit/j2/000001.json", "c")
    with pytest.raises(ObjectExistsError):
        objects.put_if_absent("audit/j1/000001.json", "changed")
    assert objects.list_names("audit/j1/") == ["audit/j1/000001.json", "audit/j1/000002.json"]
    assert objects.read("audit/j1/000001.json") == "a"
    with pytest.raises(KeyError):
        objects.read("nope")


def test_the_hash_chain_works_on_delta_objects():
    log = AuditLog(DeltaObjects(FakeDelta(), SCHEMA))
    log.append("j1", "JOB_SUBMITTED", at=NOW.isoformat(), actor="u")
    log.append("j1", "RUN_STARTED", at=NOW.isoformat(), actor="system")
    assert log.verify_chain("j1").ok and log.verify_chain("j1").events == 2


def test_the_table_definitions_make_the_audit_table_append_only():
    ddl = create_tables_sql(SCHEMA)
    audit = next(s for s in ddl if "audit_objects" in s)
    assert "delta.appendOnly" in audit and len(ddl) == 3
