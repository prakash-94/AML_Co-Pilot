"""Glue between a Databricks job and the rest of the code: secrets, settings and the Delta-backed services."""

import os
from collections.abc import Callable, Iterable, MutableMapping
from typing import Any

from aml_copilot.errors import ConfigError
from aml_copilot.service.audit import AuditLog
from aml_copilot.service.delta import DeltaDocuments, DeltaObjects, SqlFn, spark_sql
from aml_copilot.service.jobs import JobStore

DEFAULT_SCOPE = "aml-copilot"
DEFAULT_SCHEMA = "workspace.aml_copilot"
# Secret-scope key == environment variable name == the Settings field (case-insensitive).
SECRET_NAMES = ("SF_PASSWORD", "SF_SECURITY_TOKEN", "SUPABASE_DB_URL", "DATABRICKS_CLIENT_SECRET")

RoleLookup = Callable[[str], str | None]


def _secret_or_none(dbutils: Any, scope: str, key: str) -> str | None:
    try:
        return dbutils.secrets.get(scope=scope, key=key)
    except Exception:  # noqa: BLE001  (a missing key is reported by name by the caller, with nothing that could leak)
        return None


def load_secrets(
    dbutils: Any, scope: str = DEFAULT_SCOPE, names: Iterable[str] = SECRET_NAMES,
    environ: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Copy secrets from the Databricks secret scope into the environment. Returns the NAMES loaded - never values."""
    env = os.environ if environ is None else environ
    loaded = []
    for name in names:
        value = _secret_or_none(dbutils, scope, name)
        if value:
            env[name] = value
            loaded.append(name)
    return loaded


def apply_config(pairs: Iterable[str], environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Apply `KEY=VALUE` settings (non-secret configuration passed as job parameters) to the environment."""
    env = os.environ if environ is None else environ
    keys = []
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise ConfigError(f"Not a KEY=VALUE setting: {pair!r}")
        env[key.strip().upper()] = value
        keys.append(key.strip().upper())
    return keys


def reviewer_roles(sql: SqlFn, schema: str = DEFAULT_SCHEMA) -> RoleLookup:
    """Look a person's role up in the `reviewers` table (case-insensitive on the email)."""

    def lookup(email: str) -> str | None:
        rows = sql(f"SELECT role FROM {schema}.reviewers WHERE lower(email) = lower(:e) LIMIT 1", {"e": email})
        return rows[0]["role"] if rows else None

    return lookup


def delta_services(sql: SqlFn, schema: str = DEFAULT_SCHEMA) -> tuple[JobStore, AuditLog, RoleLookup]:
    return (
        JobStore(DeltaDocuments(sql, schema)),
        AuditLog(DeltaObjects(sql, schema)),
        reviewer_roles(sql, schema),
    )


def spark_services(spark: Any, schema: str = DEFAULT_SCHEMA) -> tuple[JobStore, AuditLog, RoleLookup]:
    return delta_services(spark_sql(spark), schema)
