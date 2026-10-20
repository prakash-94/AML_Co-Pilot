"""Give the web app's own identity the table access it needs (run once after the app is first created).

    python scripts/grant_app_access.py

The app runs as a service principal that Databricks creates for it. It may read job records and the reviewers list and
add audit events - nothing else. (Its warehouse and job permissions come from the bundle.)
Uses the DATABRICKS_TOKEN in your .env.
"""

import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aml_copilot.config import Settings
from aml_copilot.ingestion.sources import DatabricksSource
from aml_copilot.service.delta import connector_sql

APP_NAME = "aml-copilot"
SCHEMA = "workspace.aml_copilot"
GRANTS = [
    ("USE CATALOG", "CATALOG workspace"),
    ("USE SCHEMA", f"SCHEMA {SCHEMA}"),
    ("SELECT, MODIFY", f"TABLE {SCHEMA}.documents"),
    ("SELECT, MODIFY", f"TABLE {SCHEMA}.audit_objects"),
    ("SELECT", f"TABLE {SCHEMA}.reviewers"),
]


def main() -> int:
    values = dotenv_values(ROOT / ".env")
    host, token = values["DATABRICKS_SERVER_HOSTNAME"], values["DATABRICKS_TOKEN"]
    app = httpx.get(f"https://{host}/api/2.0/apps/{APP_NAME}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    app.raise_for_status()
    principal = app.json()["service_principal_client_id"]
    sql = connector_sql(DatabricksSource(Settings(databricks_client_id="", databricks_client_secret=""))._open)
    for privilege, target in GRANTS:
        sql(f"GRANT {privilege} ON {target} TO `{principal}`", {})
        print(f"granted {privilege} on {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
