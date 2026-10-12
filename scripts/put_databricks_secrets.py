"""Copy your credentials from .env into the Databricks secret scope `aml-copilot` (run this yourself).

    python scripts/put_databricks_secrets.py [--region us-east-2] [--dry-run]

* SF_PASSWORD and SF_SECURITY_TOKEN go across unchanged. There is no model API key: Gemini is reached through a keyless
  Google login (see docs/databricks.md).
* SUPABASE_DB_URL is converted to Supabase's *pooler* address. The direct host (db.<ref>.supabase.co) is IPv6-only and
  Databricks serverless compute has no IPv6 route to it; the pooler (aws-0/aws-1-<region>.pooler.supabase.com) is IPv4.
  The script tries both pooler names with your real password and keeps the one that connects.
* Values are sent to Databricks over HTTPS and never printed; only secret NAMES and the pooler host are shown.
Needs DATABRICKS_SERVER_HOSTNAME and DATABRICKS_TOKEN in .env (the same ones the bundle deploy uses).
"""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
SCOPE = "aml-copilot"
PLAIN = ("SF_PASSWORD", "SF_SECURITY_TOKEN")


def pooler_candidates(direct_url: str, region: str) -> list[str]:
    """Pooler connection strings for a direct Supabase URL: user becomes postgres.<ref>, host the pooler."""
    parsed = urlparse(direct_url)
    host = parsed.hostname or ""
    if not host.startswith("db.") or not host.endswith(".supabase.co"):
        return [direct_url]  # already a pooler (or custom) address - use as is
    ref = host.split(".")[1]
    user = (parsed.username or "postgres").split(".")[0]
    password = parsed.password or ""
    out = []
    for cluster in ("aws-0", "aws-1"):
        netloc = f"{user}.{ref}:{password}@{cluster}-{region}.pooler.supabase.com:5432"
        out.append(urlunparse(parsed._replace(netloc=netloc)))
    return out


def first_that_connects(candidates: list[str]) -> str | None:
    import psycopg2

    for dsn in candidates:
        try:
            psycopg2.connect(dsn, connect_timeout=10).close()
            print(f"  supabase pooler works: {urlparse(dsn).hostname}")
            return dsn
        except Exception as exc:  # noqa: BLE001  (try the next name; report only the error type)
            print(f"  supabase pooler {urlparse(dsn).hostname}: {type(exc).__name__}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default="us-east-2", help="your Supabase project's region")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    values = dotenv_values(ROOT / ".env")
    host, token = values.get("DATABRICKS_SERVER_HOSTNAME"), values.get("DATABRICKS_TOKEN")
    if not host or not token:
        print("DATABRICKS_SERVER_HOSTNAME and DATABRICKS_TOKEN must be set in .env")
        return 1

    secrets = {name: values.get(name) or "" for name in PLAIN}
    if values.get("SUPABASE_DB_URL"):
        secrets["SUPABASE_DB_URL"] = first_that_connects(pooler_candidates(values["SUPABASE_DB_URL"], args.region)) or ""

    failed = 0
    for name, value in secrets.items():
        if not value:
            print(f"  skip   {name}: not available")
            failed += name == "SUPABASE_DB_URL"
            continue
        if args.dry_run:
            print(f"  would store {name} ({len(value)} characters)")
            continue
        response = httpx.post(
            f"https://{host}/api/2.0/secrets/put", headers={"Authorization": f"Bearer {token}"}, timeout=30,
            json={"scope": SCOPE, "key": name, "string_value": value},
        )
        print(f"  {'stored' if response.status_code == 200 else 'FAILED'} {name}")
        failed += response.status_code != 200
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
