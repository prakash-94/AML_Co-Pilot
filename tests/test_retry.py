"""Retry behaviour for the Databricks and Salesforce sources (fake drivers, no sleeping)."""

import pytest
from databricks.sql import exc as dbx
from pydantic import SecretStr
from simple_salesforce import exceptions as sfx

from aml_copilot.config import Settings
from aml_copilot.errors import ConfigError, SchemaError, SourceUnavailableError
from aml_copilot.ingestion.retry import (
    RetryPolicy,
    call_with_retry,
    is_transient_databricks,
    is_transient_salesforce,
)
from aml_copilot.ingestion.sources import DatabricksSource, SalesforceSource

ACC = "ACC-S-00000"
FAST = RetryPolicy(attempts=3, initial_wait=0.0, max_wait=0.0)  # same logic, no waiting


# ------------------------------------------------------------------ the generic helper
def flaky(failures, error):
    """A callable that raises `error` `failures` times, then returns 'ok'; records its calls."""
    calls = []

    def operation():
        calls.append(1)
        if len(calls) <= failures:
            raise error
        return "ok"

    return operation, calls


def test_a_transient_failure_is_retried_until_it_succeeds():
    operation, calls = flaky(2, ConnectionError("reset"))
    result = call_with_retry(operation, source="x", policy=FAST, is_transient=lambda e: True)
    assert result == "ok" and len(calls) == 3


def test_a_permanent_failure_is_not_retried():
    operation, calls = flaky(5, ValueError("bad"))
    with pytest.raises(ValueError):
        call_with_retry(operation, source="x", policy=FAST, is_transient=lambda e: False)
    assert len(calls) == 1


def test_when_every_attempt_fails_the_original_error_is_raised():
    operation, calls = flaky(99, ConnectionError("reset"))
    with pytest.raises(ConnectionError, match="reset"):
        call_with_retry(operation, source="x", policy=FAST, is_transient=lambda e: True)
    assert len(calls) == 3


# -------------------------------------------------------------- Databricks classification
def request_error(message="boom", http_code=None):
    context = {} if http_code is None else {"http-code": http_code}
    return dbx.RequestError(message, context)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionError("reset"), True),
        (TimeoutError(), True),
        (request_error(http_code=None), True),  # no response at all
        (request_error(http_code=503), True),
        (request_error("BAD_REQUEST: Cannot create the resource, please try again later.", 400), True),
        (request_error("Invalid access token", 401), False),  # bad token: retrying cannot fix it
        (request_error("Forbidden", 403), False),
        (request_error("bad request", 400), False),
        (dbx.MaxRetryDurationError("gave up"), False),  # the driver already used its own budget
        (dbx.NonRecoverableNetworkError("501"), False),
        (dbx.ServerOperationError("syntax error"), False),
        (ValueError("anything else"), False),
        (SchemaError("no account_id"), False),
        (ConfigError("missing token"), False),
    ],
)
def test_databricks_classification(error, expected):
    assert is_transient_databricks(error) is expected


# -------------------------------------------------------------- Salesforce classification
def sf_error(cls, status):
    return cls("https://x", status, "query", b"content")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionError("reset"), True),
        (sf_error(sfx.SalesforceGeneralError, 503), True),
        (sf_error(sfx.SalesforceGeneralError, 429), True),
        (sf_error(sfx.SalesforceGeneralError, 418), False),
        (sf_error(sfx.SalesforceExpiredSession, 401), True),
        (sf_error(sfx.SalesforceResourceNotFound, 404), True),
        # Never retried: a repeated bad login can lock the account; the others cannot be fixed by waiting.
        (sfx.SalesforceAuthenticationFailed("INVALID_LOGIN", "bad password"), False),
        (sf_error(sfx.SalesforceMalformedRequest, 400), False),
        (sf_error(sfx.SalesforceRefusedRequest, 403), False),
        (ValueError("anything else"), False),
    ],
)
def test_salesforce_classification(error, expected):
    assert is_transient_salesforce(error) is expected


def test_requests_network_errors_count_as_transient_for_salesforce():
    import requests

    assert is_transient_salesforce(requests.exceptions.ConnectionError("down"))
    assert is_transient_salesforce(requests.exceptions.ReadTimeout("slow"))


# ----------------------------------------------------------------- Databricks source
class Cursor:
    def __init__(self): self.description = [("account_id",), ("amount",)]
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): pass
    def fetchall(self): return [(ACC, 9500)]


class Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return Cursor()


def databricks_settings():
    return Settings(
        _env_file=None, databricks_server_hostname="h", databricks_http_path="/p",
        databricks_token=SecretStr("t"),
    )


def flaky_connect(failures, error):
    calls = []

    def connect(**kw):
        calls.append(1)
        if len(calls) <= failures:
            raise error
        return Conn()

    return connect, calls


def test_databricks_fetch_recovers_from_a_transient_failure():
    connect, calls = flaky_connect(2, ConnectionError("reset"))
    result = DatabricksSource(databricks_settings(), connect=connect, retry=FAST).fetch(ACC)
    assert result.rows == [{"account_id": ACC, "amount": 9500}]
    assert len(calls) == 3  # two failures, then a fresh connection that worked


def test_databricks_fetch_around_recovers_too():
    connect, calls = flaky_connect(1, request_error("try again later", 400))
    src = DatabricksSource(databricks_settings(), connect=connect, retry=FAST)
    src._columns = ["account_id", "counterparty_account"]
    assert src.fetch_around([ACC], limit=10)
    assert len(calls) == 2


def test_databricks_gives_up_as_source_unavailable_after_the_last_attempt():
    connect, calls = flaky_connect(99, ConnectionError("reset"))
    src = DatabricksSource(databricks_settings(), connect=connect, retry=FAST)
    with pytest.raises(SourceUnavailableError, match="reset"):
        src.fetch(ACC)
    assert len(calls) == 3


def test_databricks_bad_token_fails_immediately():
    connect, calls = flaky_connect(99, request_error("Invalid access token", 401))
    with pytest.raises(SourceUnavailableError):
        DatabricksSource(databricks_settings(), connect=connect, retry=FAST).fetch(ACC)
    assert len(calls) == 1


def test_databricks_missing_credentials_are_not_retried():
    src = DatabricksSource(Settings(_env_file=None), retry=FAST)
    with pytest.raises(ConfigError):
        src.fetch(ACC)


# ----------------------------------------------------------------- Salesforce source
def sf_settings():
    return Settings(
        _env_file=None, sf_username="u", sf_password=SecretStr("p"), sf_security_token=SecretStr("t")
    )


class GoodSF:
    def query(self, soql):
        return {"records": [{"attributes": {}, "Account_Id__c": ACC}]}


def test_salesforce_fetch_recovers_from_a_transient_failure():
    calls = []

    def factory(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise sf_error(sfx.SalesforceGeneralError, 503)
        return GoodSF()

    result = SalesforceSource(sf_settings(), client_factory=factory, retry=FAST).fetch(ACC)
    assert result.rows == [{"Account_Id__c": ACC}]
    assert len(calls) == 2


def test_salesforce_bad_login_is_tried_exactly_once():
    calls = []

    def factory(**kw):
        calls.append(1)
        raise sfx.SalesforceAuthenticationFailed("INVALID_LOGIN", "bad password")

    with pytest.raises(SourceUnavailableError):
        SalesforceSource(sf_settings(), client_factory=factory, retry=FAST).fetch(ACC)
    assert len(calls) == 1


def test_an_expired_salesforce_session_triggers_a_fresh_login():
    logins = []

    class ExpiringSF:
        def query(self, soql):
            raise sf_error(sfx.SalesforceExpiredSession, 401)

    def factory(**kw):
        logins.append(1)
        return ExpiringSF() if len(logins) == 1 else GoodSF()

    result = SalesforceSource(sf_settings(), client_factory=factory, retry=FAST).fetch(ACC)
    assert len(logins) == 2  # the dead client was discarded and a new login made
    assert result.rows[0]["Account_Id__c"] == ACC


def test_salesforce_gives_up_as_source_unavailable_after_the_last_attempt():
    class DownSF:
        def query(self, soql):
            raise sf_error(sfx.SalesforceGeneralError, 503)

    with pytest.raises(SourceUnavailableError):
        SalesforceSource(sf_settings(), client_factory=lambda **kw: DownSF(), retry=FAST).fetch(ACC)


# ------------------------------------------------------------------------------ Supabase
def supabase_settings():
    return Settings(_env_file=None, supabase_db_url=SecretStr("postgresql://u:p@h/db"))


def test_supabase_retries_a_dropped_connection_then_succeeds():
    import psycopg2

    from aml_copilot.ingestion.sources import SupabaseSource

    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            pass

        def fetchall(self):
            return [{"case_id": "C1"}]

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    def connect(dsn):
        calls.append(1)
        if len(calls) == 1:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        return Conn()

    result = SupabaseSource(supabase_settings(), connect=connect, retry=FAST).fetch(ACC)
    assert result.rows == [{"case_id": "C1"}] and len(calls) == 2


def test_supabase_does_not_retry_a_wrong_password():
    import psycopg2

    from aml_copilot.ingestion.retry import is_transient_supabase
    from aml_copilot.ingestion.sources import SupabaseSource

    calls = []

    def connect(dsn):
        calls.append(1)
        raise psycopg2.OperationalError('FATAL: password authentication failed for user "postgres"')

    with pytest.raises(SourceUnavailableError):
        SupabaseSource(supabase_settings(), connect=connect, retry=FAST).fetch(ACC)
    assert len(calls) == 1
    assert is_transient_supabase(psycopg2.OperationalError("timeout expired"))
    assert not is_transient_supabase(ValueError("bad"))
