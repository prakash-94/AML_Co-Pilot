"""Validate or deploy the Databricks bundle using the connection settings in your .env (nothing is printed).

    python scripts/dbx_deploy.py validate
    python scripts/dbx_deploy.py deploy
    python scripts/dbx_deploy.py run investigate --account-id ACC-H-00000
    python scripts/dbx_deploy.py <any other databricks bundle subcommand and flags>

Reads DATABRICKS_SERVER_HOSTNAME / DATABRICKS_HTTP_PATH / DATABRICKS_TOKEN / SF_USERNAME from .env and exposes them to
the CLI as environment variables for this one command. The deploy token is yours; the jobs themselves read the
service principal's secret from the Databricks secret scope.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CLIENT_ID = "c3d53597-a3f2-45be-bc71-6077a0a7f77f"


def find_cli() -> str | None:
    """On PATH, or where winget puts it (a terminal opened before the install does not see the new PATH)."""
    found = shutil.which("databricks") or shutil.which("databricks.exe")
    if found:
        return found
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    matches = sorted(base.glob("Databricks.DatabricksCLI*/databricks.exe"))
    return str(matches[0]) if matches else None


def main(argv: list[str]) -> int:
    cli = find_cli()
    if not cli:
        print("The Databricks CLI was not found (winget install Databricks.DatabricksCLI).")
        return 1
    values = dotenv_values(ROOT / ".env")
    host = (values.get("DATABRICKS_SERVER_HOSTNAME") or "").strip()
    token = (values.get("DATABRICKS_TOKEN") or "").strip()
    if not host or not token:
        print("DATABRICKS_SERVER_HOSTNAME and DATABRICKS_TOKEN must be set in .env")
        return 1
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),  # `python -m build` finds this Python
        "DATABRICKS_HOST": f"https://{host}",
        "DATABRICKS_TOKEN": token,
        "BUNDLE_VAR_server_hostname": host,
        "BUNDLE_VAR_warehouse_http_path": values.get("DATABRICKS_HTTP_PATH") or "",
        "BUNDLE_VAR_gcp_project": values.get("GCP_PROJECT") or "",
        "BUNDLE_VAR_gcp_wif_audience": values.get("GCP_WIF_AUDIENCE") or "",
        "BUNDLE_VAR_gcp_service_account": values.get("GCP_SERVICE_ACCOUNT") or "",
        "BUNDLE_VAR_sf_username": values.get("SF_USERNAME") or "",
        "BUNDLE_VAR_databricks_client_id": values.get("DATABRICKS_CLIENT_ID") or DEFAULT_CLIENT_ID,
    }
    command = argv or ["validate"]
    if command[0] in ("validate", "deploy", "run", "summary", "destroy", "plan"):
        command = ["bundle", *command]
    return subprocess.run([cli, *command], cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
