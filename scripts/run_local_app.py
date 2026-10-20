"""Run the web page on your own machine against the real Databricks workspace (for trying changes before deploying).

    python scripts/run_local_app.py            # then open http://localhost:8000

It signs in to Databricks with the DATABRICKS_TOKEN in your .env and acts as the reviewer named by `--user`
(default: the owner of that token). The deployed app instead takes the reviewer from Databricks' own sign-in.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aml_copilot.config import Settings
from aml_copilot.dbx.appmain import _cached
from aml_copilot.dbx.jobs_api import JobsApiLauncher
from aml_copilot.dbx.runtime import DEFAULT_SCHEMA, delta_services
from aml_copilot.dbx.webapp import create_app
from aml_copilot.ingestion.sources import DatabricksSource
from aml_copilot.service.delta import connector_sql


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--user", default="", help="reviewer email (default: whoever owns your token)")
    args = parser.parse_args()

    settings = Settings(databricks_client_id="", databricks_client_secret="")  # personal token, not the service principal
    sql = connector_sql(DatabricksSource(settings)._open)
    store, audit, roles = delta_services(sql, DEFAULT_SCHEMA)
    user = args.user or sql("SELECT current_user() AS u", {})[0]["u"]

    host, token = settings.databricks_server_hostname, settings.databricks_token.get_secret_value()
    jobs = httpx_get_job_id(host, token)
    launcher = JobsApiLauncher(host, lambda: token, jobs)
    app = create_app(store=store, audit=audit, roles=_cached(roles, 30.0), launcher=launcher,
                     clock=lambda: datetime.now(UTC), dev_user=user)
    print(f"Open http://localhost:{args.port} (acting as {user})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def httpx_get_job_id(host: str, token: str) -> int:
    import httpx

    response = httpx.get(f"https://{host}/api/2.1/jobs/list", params={"name": "aml-investigate"},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
    response.raise_for_status()
    return int(response.json()["jobs"][0]["job_id"])


if __name__ == "__main__":
    sys.exit(main())
