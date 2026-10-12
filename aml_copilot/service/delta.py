"""The service's storage on Databricks: Delta tables instead of Firestore and Cloud Storage.

Two small adapters let the unchanged service rules (`JobStore`, `AuditLog`, `process_job`) run on Delta:

* `DeltaDocuments` is a `DocumentBackend`: one table `documents(collection, doc_id, version, body)`.
  A "transaction" reads the documents, lets the rule decide, then writes them back with ONE `MERGE` that only
  succeeds if every document still has the version that was read (optimistic concurrency). The investigate job
  allows one run at a time, so submit/finish never race each other; a lost race is detected, never silently applied.
* `DeltaObjects` is an `ObjectBackend` for the audit trail: table `audit_objects(name, data)` created with
  `delta.appendOnly = true`, so Delta itself refuses UPDATE and DELETE; a name can be inserted only once.

Both talk to Delta through `SqlFn(statement, params) -> rows` - on Databricks that is `spark.sql`, elsewhere the SQL
connector - so the logic is testable without a workspace.
"""

import json
import re
from collections.abc import Callable
from typing import Any

from aml_copilot.errors import AMLCopilotError
from aml_copilot.service.audit import ObjectExistsError
from aml_copilot.service.jobs import Key, TransactFn

SqlFn = Callable[[str, dict[str, Any]], list[dict[str, Any]]]
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")


class TransactionConflictError(AMLCopilotError):
    """Another writer changed a document between this transaction's read and write; nothing was written."""


class PartialWriteError(AMLCopilotError):
    """Some - but not all - documents of one transaction were written. Needs a human look; never retried."""


def _table(name: str) -> str:
    if not _TABLE.match(name):
        raise ValueError(f"Not a plain table name: {name!r}")
    return name


def create_tables_sql(schema: str) -> list[str]:
    """DDL for every table the service uses (idempotent)."""
    schema = _table(schema)
    documents = (
        f"CREATE TABLE IF NOT EXISTS {schema}.documents "
        "(collection STRING, doc_id STRING, version BIGINT, body STRING, updated_at TIMESTAMP)"
    )
    audit = (
        f"CREATE TABLE IF NOT EXISTS {schema}.audit_objects (name STRING, data STRING, created_at TIMESTAMP) "
        "TBLPROPERTIES ('delta.appendOnly' = 'true')"
    )
    reviewers = f"CREATE TABLE IF NOT EXISTS {schema}.reviewers (email STRING, role STRING)"
    return [documents, audit, reviewers]


def _affected(rows: list[dict[str, Any]]) -> int:
    """Rows written by a DML statement (Spark and the SQL connector both return `num_affected_rows`)."""
    return int(rows[0].get("num_affected_rows", 0)) if rows else 0


class DeltaDocuments:
    def __init__(self, sql: SqlFn, schema: str, attempts: int = 3):
        self._sql = sql
        self._table = f"{_table(schema)}.documents"
        self._attempts = attempts

    def get(self, collection: str, doc_id: str) -> dict | None:
        rows = self._sql(
            f"SELECT body FROM {self._table} WHERE collection = :c AND doc_id = :d", {"c": collection, "d": doc_id}
        )
        return json.loads(rows[0]["body"]) if rows else None

    def transact(self, keys: list[Key], fn: TransactFn) -> None:
        for attempt in range(1, self._attempts + 1):
            try:
                self._transact_once(keys, fn)
                return
            except TransactionConflictError:
                if attempt == self._attempts:
                    raise

    def _transact_once(self, keys: list[Key], fn: TransactFn) -> None:
        where = " OR ".join(f"(collection = :c{i} AND doc_id = :d{i})" for i in range(len(keys)))
        params = {f"{p}{i}": v for i, key in enumerate(keys) for p, v in (("c", key[0]), ("d", key[1]))}
        rows = self._sql(f"SELECT collection, doc_id, version, body FROM {self._table} WHERE {where}", params)
        found = {(r["collection"], r["doc_id"]): (int(r["version"]), json.loads(r["body"])) for r in rows}

        writes = fn({key: (found[key][1] if key in found else None) for key in keys})
        if not writes:
            return

        values, bind = [], {}
        for i, (key, document) in enumerate(writes.items()):
            values.append(f"(:wc{i}, :wd{i}, CAST(:we{i} AS BIGINT), :wb{i})")
            bind.update({f"wc{i}": key[0], f"wd{i}": key[1], f"we{i}": found[key][0] if key in found else 0,
                         f"wb{i}": json.dumps(document, separators=(",", ":"))})
        statement = (
            f"MERGE INTO {self._table} AS t "
            f"USING (SELECT * FROM VALUES {', '.join(values)} AS s(collection, doc_id, expected, body)) AS s "
            "ON t.collection = s.collection AND t.doc_id = s.doc_id "
            "WHEN MATCHED AND t.version = s.expected THEN UPDATE SET t.body = s.body, "
            "t.version = t.version + 1, t.updated_at = current_timestamp() "
            "WHEN NOT MATCHED AND s.expected = 0 THEN INSERT (collection, doc_id, version, body, updated_at) "
            "VALUES (s.collection, s.doc_id, 1, s.body, current_timestamp())"
        )
        written = _affected(self._sql(statement, bind))
        if written == len(writes):
            return
        if written == 0:
            raise TransactionConflictError("a document changed during the transaction")
        raise PartialWriteError(f"wrote {written} of {len(writes)} documents")

    def list_recent(self, collection: str, order_by: str, limit: int) -> list[dict]:
        if not _FIELD.match(order_by):
            raise ValueError(f"Not a plain field name: {order_by!r}")
        rows = self._sql(
            f"SELECT body FROM {self._table} WHERE collection = :c "
            f"ORDER BY get_json_object(body, '$.{order_by}') DESC LIMIT {int(limit)}",
            {"c": collection},
        )
        return [json.loads(r["body"]) for r in rows]


class DeltaObjects:
    def __init__(self, sql: SqlFn, schema: str):
        self._sql = sql
        self._table = f"{_table(schema)}.audit_objects"

    def put_if_absent(self, name: str, data: str) -> None:
        rows = self._sql(
            f"INSERT INTO {self._table} SELECT :n, :d, current_timestamp() "
            f"WHERE NOT EXISTS (SELECT 1 FROM {self._table} WHERE name = :n)",
            {"n": name, "d": data},
        )
        if _affected(rows) == 0:
            raise ObjectExistsError(name)

    def list_names(self, prefix: str) -> list[str]:
        rows = self._sql(f"SELECT name FROM {self._table} WHERE startswith(name, :p) ORDER BY name", {"p": prefix})
        return [r["name"] for r in rows]

    def read(self, name: str) -> str:
        rows = self._sql(f"SELECT data FROM {self._table} WHERE name = :n", {"n": name})
        if not rows:
            raise KeyError(name)
        return rows[0]["data"]


# ------------------------------------------------------------------------------ ways to run SQL
def spark_sql(spark: Any) -> SqlFn:
    """Run statements with the notebook's / job's own Spark session (named parameters, no warehouse needed)."""

    def run(statement: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [row.asDict() for row in spark.sql(statement, args=params or None).collect()]

    return run


def connector_sql(open_connection: Callable[[], Any]) -> SqlFn:
    """Run statements over the Databricks SQL connector (from a laptop, CI or any other machine)."""

    def run(statement: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        connection = open_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement, params or None)
                if cursor.description is None:
                    return []
                names = [d[0] for d in cursor.description]
                return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        finally:
            connection.close()

    return run


__all__ = [
    "DeltaDocuments", "DeltaObjects", "PartialWriteError", "SqlFn", "TransactionConflictError", "connector_sql",
    "create_tables_sql", "spark_sql",
]
