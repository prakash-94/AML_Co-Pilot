"""Network expansion tests - fake transaction store, no live credentials."""

import pytest
from pydantic import SecretStr

from aml_copilot.config import Settings
from aml_copilot.errors import SchemaError, SourceUnavailableError
from aml_copilot.ingestion import ExpansionConfig, expand_network, run_ingestion
from aml_copilot.ingestion.sources import DatabricksSource, FetchResult

ROOT = "ACC-L-00000"
L0, L1, L2 = "ACC-LC-00000-0", "ACC-LC-00000-1", "ACC-LC-00000-2"


def wire(tid, src, dst, amount, **extra):
    return {
        "transaction_id": tid, "account_id": src, "counterparty_account": dst,
        "amount": amount, "type": "wire_transfer", "timestamp": f"2026-08-0{tid[-1]}", **extra,
    }


# the real layering chain: shrinking amounts hop by hop
CHAIN = [
    wire("t1", ROOT, L0, 44587.98),
    wire("t2", L0, L1, 40010.80),
    wire("t3", L1, L2, 35894.25),
]


class FakeStore:
    """Stands in for Databricks: returns rows where any id is sender or counterparty."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def fetch_around(self, ids, limit):
        self.queries.append(list(ids))
        hit = [r for r in self.rows if r["account_id"] in ids or r["counterparty_account"] in ids]
        return [dict(r) for r in hit][:limit]


def test_finds_the_full_layering_chain_with_hops():
    store = FakeStore(CHAIN)
    result = expand_network(ROOT, store.fetch_around)

    assert {(a.account_id, a.hop) for a in result.related_accounts} == {(L0, 1), (L1, 2), (L2, 3)}
    # the root's own wire (t1) is a primary row, so only the downstream hops are "related"
    assert {r["transaction_id"]: r["hop"] for r in result.related_transactions} == {"t2": 2, "t3": 3}
    assert result.complete and result.warnings == []


def test_each_hop_is_one_query_and_stops_when_the_chain_ends():
    store = FakeStore(CHAIN)
    expand_network(ROOT, store.fetch_around)
    assert store.queries == [[ROOT], [L0], [L1], [L2]]


def test_inbound_transfers_to_the_account_are_found():
    inbound = wire("t9", "ACC-S-00777", ROOT, 5000)
    result = expand_network(ROOT, FakeStore([inbound]).fetch_around)
    assert [a.account_id for a in result.related_accounts] == ["ACC-S-00777"]
    assert [r["transaction_id"] for r in result.related_transactions] == ["t9"]


def test_max_hops_stops_and_says_so():
    result = expand_network(ROOT, FakeStore(CHAIN).fetch_around, ExpansionConfig(max_hops=2))
    assert {a.account_id for a in result.related_accounts} == {L0, L1}
    assert not result.complete
    assert any("stopped at 2 hops" in w for w in result.warnings)


def test_account_cap_stops_and_says_so():
    fan_out = [wire(f"t{i}", ROOT, f"ACC-N-{i:05d}", 100 + i) for i in range(1, 9)]
    result = expand_network(ROOT, FakeStore(fan_out).fetch_around, ExpansionConfig(max_accounts=3))
    assert len(result.related_accounts) == 3
    assert not result.complete
    assert any("capped at 3 related accounts" in w for w in result.warnings)


def test_row_cap_truncates_and_says_so():
    many = [wire(f"t{i}", f"ACC-N-{i:05d}", ROOT, 10) for i in range(1, 9)]
    result = expand_network(ROOT, FakeStore(many).fetch_around, ExpansionConfig(max_rows=5))
    assert len(result.related_transactions) == 5
    assert not result.complete and any("truncated to 5 rows" in w for w in result.warnings)


def test_only_wires_extend_the_chain():
    cash = {"transaction_id": "t5", "account_id": ROOT, "counterparty_account": "ACC-N-00001",
            "amount": 9000, "type": "cash_deposit", "timestamp": "2026-08-05"}
    result = expand_network(ROOT, FakeStore([cash]).fetch_around)
    assert result.related_accounts == []  # a cash row does not pull in its counterparty


def test_duplicate_rows_are_deduplicated_by_transaction_id():
    dup = CHAIN + [dict(CHAIN[1])]
    result = expand_network(ROOT, FakeStore(dup).fetch_around)
    assert [r["transaction_id"] for r in result.related_transactions].count("t2") == 1


def test_ground_truth_never_survives_expansion():
    leaky = [wire("t2", L0, L1, 1.0, ground_truth_label="layering")]
    result = expand_network(L1, FakeStore(leaky).fetch_around)  # t2 reaches L1 as inbound
    assert result.related_transactions, "the leaky row must be present for this test to mean anything"
    assert all("ground_truth_label" not in r for r in result.related_transactions)


def test_outage_mid_expansion_keeps_partial_results():
    class Flaky(FakeStore):
        def fetch_around(self, ids, limit):
            if len(self.queries) == 2:
                raise SourceUnavailableError("databricks", "timeout")
            return super().fetch_around(ids, limit)

    result = expand_network(ROOT, Flaky(CHAIN).fetch_around)
    assert not result.complete
    assert any("stopped at hop 3" in w for w in result.warnings)
    assert {a.account_id for a in result.related_accounts} == {L0, L1}  # what was found is kept


# ----------------------------------------------------------- pipeline
def pipeline(store, **kw):
    class Src(FakeStore):
        name = "databricks"

        def fetch(self, account_id):
            return FetchResult([r for r in self.rows if r["account_id"] == account_id])

    profile = {"Name": "X", "Account_Id__c": ROOT, "Occupation__c": "Teacher",
               "Risk_Rating__c": "High", "Is_PEP__c": True, "KYC_Last_Reviewed__c": "2025-11-02"}

    class Static:
        def __init__(self, name, rows):
            self.name, self._rows = name, rows

        def fetch(self, account_id):
            return FetchResult(self._rows)

    return run_ingestion(
        ROOT,
        transactions_source=Src(store.rows),
        profile_source=Static("salesforce", [profile]),
        cases_source=Static("supabase", []),
        **kw,
    )


def test_pipeline_off_by_default_keeps_primary_rows_only():
    bundle = pipeline(FakeStore(CHAIN))
    assert [t["transaction_id"] for t in bundle.transactions] == ["t1"]
    assert bundle.related_transactions == [] and bundle.network_expanded is False


def test_pipeline_with_expansion_adds_related_evidence_without_touching_primary():
    bundle = pipeline(FakeStore(CHAIN), expansion=ExpansionConfig())
    assert [t["transaction_id"] for t in bundle.transactions] == ["t1"]
    assert [r["transaction_id"] for r in bundle.related_transactions] == ["t2", "t3"]
    assert [(a.account_id, a.hop) for a in bundle.related_accounts] == [(L0, 1), (L1, 2), (L2, 3)]
    assert bundle.network_expanded and bundle.warnings == []


def test_pipeline_reports_bounds_as_warnings():
    bundle = pipeline(FakeStore(CHAIN), expansion=ExpansionConfig(max_hops=1))
    assert any("stopped at 1 hops" in w for w in bundle.warnings)


# --------------------------------------------- the Databricks query itself
class Cursor:
    def __init__(self, columns, rows, log):
        self.description = [(c,) for c in columns]
        self._rows, self.log = rows, log

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.log.append((sql, params))
    def fetchall(self): return self._rows


class Conn:
    def __init__(self, cursor): self._cursor = cursor
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self._cursor


def dbx(columns, rows, log):
    settings = Settings(_env_file=None, databricks_server_hostname="h", databricks_http_path="/p",
                        databricks_token=SecretStr("t"))
    cursor = Cursor(columns, rows, log)
    return DatabricksSource(settings, connect=lambda **kw: Conn(cursor))


def test_fetch_around_is_bidirectional_bound_and_excludes_ground_truth():
    log = []
    cols = ["account_id", "counterparty_account", "amount", "ground_truth_label"]
    src = dbx(cols, [(ROOT, L0, 1.0)], log)
    rows = src.fetch_around([ROOT, L0], 100)

    sql, params = log[-1]
    assert "account_id IN (:a0, :a1)" in sql and "counterparty_account IN (:a0, :a1)" in sql
    assert params == {"a0": ROOT, "a1": L0}
    assert ROOT not in sql and "ground_truth_label" not in sql and "LIMIT 100" in sql
    assert rows == [{"account_id": ROOT, "counterparty_account": L0, "amount": 1.0}]


def test_fetch_around_requires_the_counterparty_column():
    with pytest.raises(SchemaError):
        dbx(["account_id", "amount"], [], []).fetch_around([ROOT], 10)


def test_fetch_around_with_no_ids_does_not_query():
    log = []
    assert dbx(["account_id"], [], log).fetch_around([], 10) == [] and log == []
