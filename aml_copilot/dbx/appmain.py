"""Builds the web app from the environment a Databricks App provides (kept apart from `databricks/app/main.py` so it can be tested).

The platform gives every app its own service principal and these variables: DATABRICKS_HOST, DATABRICKS_CLIENT_ID,
DATABRICKS_CLIENT_SECRET. The bundle adds WAREHOUSE_ID and INVESTIGATE_JOB_ID from the resources it attaches
to the app. Nothing secret is written down anywhere.
"""

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from aml_copilot.config import Settings
from aml_copilot.dbx.jobs_api import JobsApiLauncher
from aml_copilot.dbx.runtime import DEFAULT_SCHEMA, delta_services
from aml_copilot.dbx.webapp import create_app
from aml_copilot.errors import ConfigError
from aml_copilot.ingestion.databricks_auth import DatabricksOAuthTokenProvider
from aml_copilot.ingestion.sources import DatabricksSource
from aml_copilot.service.delta import connector_sql

ROLE_CACHE_SECONDS = 30.0


def _cached(lookup: Callable[[str], str | None], seconds: float, clock: Callable[[], float] = time.monotonic):
    """Remember a person's role briefly: every request asks, and each lookup is a SQL round trip."""
    seen: dict[str, tuple[float, str | None]] = {}

    def cached(email: str) -> str | None:
        key = email.lower()
        hit = seen.get(key)
        if hit and clock() - hit[0] < seconds:
            return hit[1]
        value = lookup(email)
        seen[key] = (clock(), value)
        return value

    return cached


def build_from_environment(environ: Mapping[str, str], *, dev_user: str = "") -> FastAPI:
    def need(name: str) -> str:
        value = (environ.get(name) or "").strip()
        if not value:
            raise ConfigError(f"The app needs {name} (it is set by the bundle or by Databricks)")
        return value

    host = need("DATABRICKS_HOST").removeprefix("https://").rstrip("/")
    client_id, client_secret = need("DATABRICKS_CLIENT_ID"), need("DATABRICKS_CLIENT_SECRET")
    warehouse_http_path = f"/sql/1.0/warehouses/{need('WAREHOUSE_ID')}"
    settings = Settings(
        _env_file=None, databricks_server_hostname=host, databricks_http_path=warehouse_http_path,
        databricks_client_id=client_id, databricks_client_secret=client_secret,
    )
    sql = connector_sql(DatabricksSource(settings)._open)  # one connection per statement, signed in as the app
    store, audit, roles = delta_services(sql, environ.get("AML_SCHEMA") or DEFAULT_SCHEMA)
    token = DatabricksOAuthTokenProvider(host, client_id, client_secret).token
    launcher = JobsApiLauncher(host, token, int(need("INVESTIGATE_JOB_ID")))
    clock: Any = lambda: datetime.now(UTC)
    return create_app(store=store, audit=audit, roles=_cached(roles, ROLE_CACHE_SECONDS), launcher=launcher,
                      clock=clock, dev_user=dev_user)
