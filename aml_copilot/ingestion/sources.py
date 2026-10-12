"""Step 1 - raw data ingestion from the three source systems.

Each source exposes `fetch(account_id) -> FetchResult`. Connection factories are
injectable so the query-building logic can be tested without live credentials.
Driver failures are wrapped in SourceUnavailableError at this boundary.
"""

import re
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from aml_copilot.config import Settings, require
from aml_copilot.errors import AMLCopilotError, SchemaError, SourceUnavailableError
from aml_copilot.ingestion.databricks_auth import DatabricksOAuthTokenProvider
from aml_copilot.ingestion.retry import (
    DATABRICKS_RETRY,
    SALESFORCE_RETRY,
    SUPABASE_RETRY,
    RetryPolicy,
    call_with_retry,
    is_transient_databricks,
    is_transient_salesforce,
    is_transient_supabase,
)

log = structlog.get_logger()

# Labels used only to evaluate the system offline. Agents must never see them.
GROUND_TRUTH_COLUMNS = frozenset({"ground_truth_label"})

_TABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}")

CASE_HISTORY_COLUMNS = (
    "case_id", "account_id", "case_type", "opened_date",
    "closed_date", "status", "disposition", "investigator_notes",
)


_USERINFO_RE = re.compile(r"[^\s\"'/@]*@")
_URL_RE = re.compile(r"postgres(?:ql)?://\S+")


def _scrub(detail: str) -> str:
    """Strip connection-string fragments (passwords, user info) from driver errors."""
    return _USERINFO_RE.sub("***@", _URL_RE.sub("<dsn>", detail)).strip()


@dataclass
class FetchResult:
    rows: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


class Source(Protocol):
    name: str

    def fetch(self, account_id: str) -> FetchResult: ...


def select_safe_columns(all_columns: Sequence[str]) -> list[str]:
    """Drop ground-truth columns; fail loudly if the join key is absent."""
    safe = [c for c in all_columns if c.lower() not in GROUND_TRUTH_COLUMNS]
    if "account_id" not in {c.lower() for c in safe}:
        raise SchemaError("Transactions table has no account_id column")
    return safe


def _quote_table(table: str) -> str:
    if not _TABLE_RE.fullmatch(table):
        raise SchemaError(f"Unsafe table name: {table!r}")
    return ".".join(f"`{part}`" for part in table.split("."))


class DatabricksSource:
    name = "databricks"

    def __init__(
        self,
        settings: Settings,
        connect: Callable[..., Any] | None = None,
        retry: RetryPolicy = DATABRICKS_RETRY,
    ):
        self._settings = settings
        self._connect = connect
        self._retry = retry
        self._columns: list[str] | None = None  # cached safe column list

    def _run(self, work: Callable[[Any], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Open a connection, run `work(cursor)`, retrying transient failures.

        Each attempt opens a fresh connection - a broken one cannot be reused. Whatever still
        fails after the last attempt becomes SourceUnavailableError (our own errors pass through).
        """

        def attempt() -> list[dict[str, Any]]:
            with self._open() as conn, conn.cursor() as cur:
                return work(cur)

        try:
            return call_with_retry(
                attempt,
                source=self.name,
                policy=self._retry,
                is_transient=is_transient_databricks,
            )
        except AMLCopilotError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(self.name, _scrub(str(exc))) from exc

    def _open(self) -> Any:
        s = self._settings
        use_oauth = bool(s.databricks_client_id and s.databricks_client_secret.get_secret_value())
        require(s, "databricks_server_hostname", "databricks_http_path", *(() if use_oauth else ("databricks_token",)))
        if self._connect is not None:
            connect = self._connect
        else:
            from databricks import sql as databricks_sql

            connect = databricks_sql.connect
        if use_oauth:  # a service principal: short-lived tokens, renewed automatically
            provider = DatabricksOAuthTokenProvider(
                s.databricks_server_hostname, s.databricks_client_id, s.databricks_client_secret.get_secret_value()
            )
            return connect(
                server_hostname=s.databricks_server_hostname,
                http_path=s.databricks_http_path,
                credentials_provider=provider.credentials_provider(),
            )
        return connect(
            server_hostname=s.databricks_server_hostname,
            http_path=s.databricks_http_path,
            access_token=s.databricks_token.get_secret_value(),
        )

    def fetch(self, account_id: str) -> FetchResult:
        table = _quote_table(self._settings.databricks_transactions_table)
        limit = int(self._settings.transaction_row_limit)

        def work(cur: Any) -> list[dict[str, Any]]:
            columns = self._load_columns(cur, table)
            cols = ", ".join(f"`{c}`" for c in columns)
            cur.execute(
                f"SELECT {cols} FROM {table} WHERE account_id = :account_id LIMIT {limit + 1}",
                {"account_id": account_id},
            )
            return [dict(zip(columns, r)) for r in cur.fetchall()]

        rows = self._run(work)
        warnings = []
        if len(rows) > limit:
            rows = rows[:limit]
            warnings.append(f"Transactions truncated to {limit} rows")
        return FetchResult(rows, warnings)

    def _load_columns(self, cur: Any, table: str) -> list[str]:
        """Safe column list, probed once: a zero-row query reads the schema, never any data."""
        if self._columns is None:
            cur.execute(f"SELECT * FROM {table} LIMIT 0")
            self._columns = select_safe_columns([d[0] for d in cur.description])
        return self._columns

    def fetch_around(self, account_ids: Sequence[str], limit: int) -> list[dict[str, Any]]:
        """Transactions where any of `account_ids` is the sender or the counterparty.

        Powers network expansion. Ids are bound parameters; at most `limit` rows come back.
        """
        ids = list(dict.fromkeys(account_ids))
        if not ids:
            return []
        table = _quote_table(self._settings.databricks_transactions_table)
        marks = ", ".join(f":a{i}" for i in range(len(ids)))
        params = {f"a{i}": v for i, v in enumerate(ids)}

        def work(cur: Any) -> list[dict[str, Any]]:
            columns = self._load_columns(cur, table)
            if "counterparty_account" not in {c.lower() for c in columns}:
                raise SchemaError("Transactions table has no counterparty_account column")
            cols = ", ".join(f"`{c}`" for c in columns)
            cur.execute(
                f"SELECT {cols} FROM {table} WHERE account_id IN ({marks}) "
                f"OR counterparty_account IN ({marks}) LIMIT {int(limit)}",
                params,
            )
            return [dict(zip(columns, r)) for r in cur.fetchall()]

        return self._run(work)


class SalesforceSource:
    name = "salesforce"

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[..., Any] | None = None,
        retry: RetryPolicy = SALESFORCE_RETRY,
    ):
        self._settings = settings
        self._client_factory = client_factory
        self._retry = retry
        self._client: Any = None

    def _sf(self) -> Any:
        if self._client is None:
            s = self._settings
            require(s, "sf_username", "sf_password", "sf_security_token")
            if self._client_factory is not None:
                factory = self._client_factory
            else:
                from simple_salesforce import Salesforce

                factory = Salesforce
            self._client = factory(
                username=s.sf_username,
                password=s.sf_password.get_secret_value(),
                security_token=s.sf_security_token.get_secret_value(),
            )
        return self._client

    def fetch(self, account_id: str) -> FetchResult:
        from simple_salesforce.exceptions import SalesforceExpiredSession
        from simple_salesforce.format import format_soql

        soql = format_soql(
            "SELECT Name, Account_Id__c, Occupation__c, Risk_Rating__c, "
            "Is_PEP__c, KYC_Last_Reviewed__c FROM Customer_Profile__c "
            "WHERE Account_Id__c = {account_id}",
            account_id=account_id,
        )

        def attempt() -> list[dict[str, Any]]:
            try:
                return self._sf().query(soql)["records"]
            except SalesforceExpiredSession:
                self._client = None  # the cached login is dead; the next attempt logs in afresh
                raise

        try:
            records = call_with_retry(
                attempt,
                source=self.name,
                policy=self._retry,
                is_transient=is_transient_salesforce,
            )
        except AMLCopilotError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(self.name, _scrub(str(exc))) from exc
        return FetchResult([{k: v for k, v in r.items() if k != "attributes"} for r in records])


class SupabaseSource:
    name = "supabase"

    def __init__(
        self, settings: Settings, connect: Callable[..., Any] | None = None, retry: RetryPolicy = SUPABASE_RETRY
    ):
        self._settings = settings
        self._connect = connect
        self._retry = retry

    def fetch(self, account_id: str) -> FetchResult:
        require(self._settings, "supabase_db_url")
        if self._connect is not None:
            connect = self._connect
        else:
            import psycopg2
            from psycopg2.extras import RealDictCursor

            def connect(dsn: str) -> Any:
                return psycopg2.connect(dsn, connect_timeout=10, cursor_factory=RealDictCursor)

        query = (
            f"SELECT {', '.join(CASE_HISTORY_COLUMNS)} FROM case_history "
            "WHERE account_id = %s ORDER BY opened_date"
        )
        def attempt() -> list[dict[str, Any]]:
            dsn = self._settings.supabase_db_url.get_secret_value()
            with closing(connect(dsn)) as conn, conn.cursor() as cur:
                cur.execute(query, (account_id,))
                return [dict(r) for r in cur.fetchall()]

        try:
            rows = call_with_retry(attempt, source=self.name, policy=self._retry, is_transient=is_transient_supabase)
        except Exception as exc:
            raise SourceUnavailableError(self.name, _scrub(str(exc))) from exc
        return FetchResult(rows)
