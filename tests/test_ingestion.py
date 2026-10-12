"""Ingestion tests - no live credentials; all source systems are faked."""

import pytest
from pydantic import SecretStr

from aml_copilot.config import Settings
from aml_copilot.errors import (
    ConfigError,
    EntityResolutionError,
    InvalidAccountIdError,
    SchemaError,
    SourceUnavailableError,
)
from aml_copilot.ids import validate_account_id
from aml_copilot.ingestion import run_ingestion
from aml_copilot.ingestion.sources import (
    DatabricksSource,
    FetchResult,
    SalesforceSource,
    SupabaseSource,
    select_safe_columns,
)

ACC = "ACC-S-00001"


# ---------------------------------------------------------------- fakes
class FakeSource:
    def __init__(self, name, rows=None, warnings=None, error=None):
        self.name = name
        self._rows = rows or []
        self._warnings = warnings or []
        self._error = error
        self.calls = []

    def fetch(self, account_id):
        self.calls.append(account_id)
        if self._error:
            raise self._error
        return FetchResult(self._rows, self._warnings)


def tx(account=ACC, **extra):
    return {"account_id": account, "amount": 9500, "txn_type": "cash_deposit", **extra}


def sf_row(account=ACC):
    return {
        "Name": "Tom Garcia", "Account_Id__c": account, "Occupation__c": "Consultant",
        "Risk_Rating__c": "High", "Is_PEP__c": False, "KYC_Last_Reviewed__c": "2026-01-15",
    }


def case_row(account=ACC, case_id=1):
    return {
        "case_id": case_id, "account_id": account, "case_type": "structuring",
        "opened_date": "2026-02-01", "closed_date": None, "status": "open",
        "disposition": None, "investigator_notes": "n/a",
    }


def run(tx_src=None, prof_src=None, case_src=None, account=ACC):
    return run_ingestion(
        account,
        transactions_source=tx_src or FakeSource("databricks", [tx()]),
        profile_source=prof_src or FakeSource("salesforce", [sf_row()]),
        cases_source=case_src or FakeSource("supabase", [case_row()]),
    )


# ---------------------------------------------------------- id validation
@pytest.mark.parametrize("raw", ["ACC-S-00001", " acc-l-12345 ", "ACC-N-99999\t", "ACC-H-00000"])
def test_valid_ids_are_canonicalised(raw):
    assert validate_account_id(raw) == raw.strip().upper()


@pytest.mark.parametrize(
    "raw",
    [
        "ACC-S-00001' OR '1'='1", "ACC-S-00001\nACC-S-00002", "ACC-X-00001",
        "ACC-S-0001", "ACC-S-٠٠٠٠١", "", None,
    ],
)
def test_invalid_ids_rejected(raw):
    with pytest.raises(InvalidAccountIdError):
        validate_account_id(raw)


def test_invalid_id_never_reaches_a_source():
    src = FakeSource("databricks")
    with pytest.raises(InvalidAccountIdError):
        run(tx_src=src, account="bad'; DROP TABLE x;--")
    assert src.calls == []


# ------------------------------------------------------- ground truth guard
def test_select_safe_columns_drops_ground_truth_case_insensitively():
    cols = select_safe_columns(["account_id", "amount", "Ground_Truth_Label"])
    assert cols == ["account_id", "amount"]


def test_select_safe_columns_requires_join_key():
    with pytest.raises(SchemaError):
        select_safe_columns(["amount", "ground_truth_label"])


def test_pipeline_strips_ground_truth_even_if_a_source_leaks_it():
    leaky = FakeSource("databricks", [tx(ground_truth_label="structuring")])
    bundle = run(tx_src=leaky)
    assert all("ground_truth_label" not in t for t in bundle.transactions)


# --------------------------------------------------------- happy path
def test_happy_path_joins_all_three_sources():
    bundle = run()
    assert bundle.account_id == ACC
    assert len(bundle.transactions) == 1
    assert bundle.profile.risk_rating == "High" and bundle.profile.is_pep is False
    assert bundle.profile.kyc_last_reviewed.isoformat() == "2026-01-15"
    assert bundle.case_history[0].case_type == "structuring"
    assert bundle.missing_sources == [] and bundle.warnings == []
    assert "ground_truth_label" not in bundle.model_dump_json()


def test_input_id_is_canonicalised_before_querying():
    src = FakeSource("databricks", [tx()])
    run(tx_src=src, account=" acc-s-00001 ")
    assert src.calls == [ACC]


# ------------------------------------------------ partial / missing data
def test_source_outage_is_recorded_not_raised():
    down = FakeSource("salesforce", error=SourceUnavailableError("salesforce", "timeout"))
    bundle = run(prof_src=down)
    assert bundle.missing_sources == ["salesforce"]
    assert bundle.profile is None
    assert len(bundle.transactions) == 1


def test_unknown_customer_is_a_warning_not_an_outage():
    bundle = run(prof_src=FakeSource("salesforce", []))
    assert bundle.profile is None
    assert bundle.missing_sources == []
    assert any("No Salesforce KYC profile" in w for w in bundle.warnings)


def test_source_warnings_are_propagated():
    bundle = run(tx_src=FakeSource("databricks", [tx()], warnings=["Transactions truncated to 5 rows"]))
    assert "Transactions truncated to 5 rows" in bundle.warnings


# ---------------------------------------------------- entity resolution
def test_mismatched_transaction_account_raises():
    with pytest.raises(EntityResolutionError, match="databricks"):
        run(tx_src=FakeSource("databricks", [tx(), tx(account="ACC-S-00002")]))


def test_mismatched_case_account_raises():
    with pytest.raises(EntityResolutionError, match="supabase"):
        run(case_src=FakeSource("supabase", [case_row(account="ACC-L-00001")]))


def test_mismatched_profile_raises():
    with pytest.raises(EntityResolutionError, match="salesforce"):
        run(prof_src=FakeSource("salesforce", [sf_row(account="ACC-N-00009")]))


def test_duplicate_profiles_are_ambiguous():
    with pytest.raises(EntityResolutionError, match="2 Salesforce profiles"):
        run(prof_src=FakeSource("salesforce", [sf_row(), sf_row()]))


def test_id_case_differences_between_sources_still_resolve():
    bundle = run(tx_src=FakeSource("databricks", [tx(account=ACC.lower())]))
    assert len(bundle.transactions) == 1


# -------------------------------------------- real sources, fake drivers
class FakeDatabricksCursor:
    def __init__(self, columns, rows, log):
        self.description = [(c,) for c in columns]
        self._rows, self.log = rows, log

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.log.append((sql, params))
    def fetchall(self): return self._rows


class FakeDatabricksConn:
    def __init__(self, cursor): self._cursor = cursor
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self._cursor


def databricks_settings(**kw):
    return Settings(
        _env_file=None, databricks_server_hostname="h", databricks_http_path="/p",
        databricks_token=SecretStr("t"), **kw,
    )


def test_databricks_query_excludes_ground_truth_and_is_parameterised():
    log = []
    cursor = FakeDatabricksCursor(["account_id", "amount", "ground_truth_label"], [(ACC, 9500)], log)
    src = DatabricksSource(databricks_settings(), connect=lambda **kw: FakeDatabricksConn(cursor))
    result = src.fetch(ACC)

    data_sql, params = log[-1]
    assert "ground_truth_label" not in data_sql
    assert ":account_id" in data_sql and ACC not in data_sql
    assert params == {"account_id": ACC}
    assert "`aml_copilot`.`transactions`" in data_sql
    assert result.rows == [{"account_id": ACC, "amount": 9500}]


def test_databricks_truncation_is_flagged():
    cursor = FakeDatabricksCursor(["account_id"], [(ACC,)] * 4, [])
    src = DatabricksSource(
        databricks_settings(transaction_row_limit=3),
        connect=lambda **kw: FakeDatabricksConn(cursor),
    )
    result = src.fetch(ACC)
    assert len(result.rows) == 3
    assert result.warnings == ["Transactions truncated to 3 rows"]


def test_databricks_rejects_unsafe_table_name():
    src = DatabricksSource(databricks_settings(databricks_transactions_table="t; DROP TABLE x"))
    with pytest.raises(SchemaError):
        src.fetch(ACC)


def test_databricks_driver_error_becomes_source_unavailable():
    def boom(**kw):
        raise OSError("connection reset")

    src = DatabricksSource(databricks_settings(), connect=boom)
    with pytest.raises(SourceUnavailableError, match="connection reset"):
        src.fetch(ACC)


def test_missing_credentials_is_a_config_error_not_an_outage():
    src = DatabricksSource(Settings(_env_file=None))
    with pytest.raises(ConfigError, match="DATABRICKS_TOKEN"):
        src.fetch(ACC)


def test_salesforce_soql_is_escaped_not_interpolated():
    seen = []

    class FakeSF:
        def query(self, soql):
            seen.append(soql)
            return {"records": [{"attributes": {"type": "x"}, **sf_row()}]}

    settings = Settings(
        _env_file=None, sf_username="u", sf_password=SecretStr("p"), sf_security_token=SecretStr("t")
    )
    result = SalesforceSource(settings, client_factory=lambda **kw: FakeSF()).fetch(ACC)
    assert seen[0].endswith(f"WHERE Account_Id__c = '{ACC}'")
    assert "attributes" not in result.rows[0]


def test_supabase_query_is_parameterised_with_explicit_columns():
    log = []

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): log.append((sql, params))
        def fetchall(self): return [case_row()]

    class Conn:
        def cursor(self): return Cur()
        def close(self): pass

    settings = Settings(_env_file=None, supabase_db_url=SecretStr("postgresql://x"))
    result = SupabaseSource(settings, connect=lambda dsn: Conn()).fetch(ACC)
    sql, params = log[0]
    assert "SELECT *" not in sql and "%s" in sql and ACC not in sql
    assert params == (ACC,)
    assert result.rows[0]["case_id"] == 1


def test_supabase_driver_errors_are_scrubbed_of_password_fragments():
    def boom(dsn):
        raise OSError('could not translate host name "s3cret@db.example.co" to address')

    settings = Settings(_env_file=None, supabase_db_url=SecretStr("postgresql://x"))
    with pytest.raises(SourceUnavailableError) as info:
        SupabaseSource(settings, connect=boom).fetch(ACC)
    assert "s3cret" not in str(info.value)
    assert "db.example.co" in str(info.value)


# ------------------------------------------------- integration layer (step 2)
def test_merge_sources_combines_the_three_pulls_without_normalizing():
    from aml_copilot.ingestion.pipeline import merge_sources

    raw = merge_sources(
        ACC,
        FetchResult([tx()], ["truncated"]),
        FetchResult([sf_row()]),
        FetchResult([case_row()]),
    )
    assert raw.transaction_rows == [tx()]
    assert raw.profile_rows[0]["Account_Id__c"] == ACC  # still Salesforce field names
    assert raw.case_rows == [case_row()]
    assert raw.missing_sources == [] and raw.warnings == ["truncated"]


def test_merge_sources_records_outages_and_keeps_going():
    from aml_copilot.ingestion.pipeline import merge_sources

    raw = merge_sources(ACC, FetchResult([tx()]), None, FetchResult([]))
    assert raw.missing_sources == ["salesforce"]
    assert raw.profile_rows == [] and raw.profile_queried is False


def test_outage_is_not_reported_as_unknown_customer():
    down = FakeSource("salesforce", error=SourceUnavailableError("salesforce", "timeout"))
    bundle = run(prof_src=down)
    assert not any("No Salesforce KYC profile" in w for w in bundle.warnings)
