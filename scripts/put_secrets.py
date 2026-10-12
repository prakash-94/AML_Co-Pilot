"""Copy the secret values from your local .env into Google Secret Manager (run this yourself).

    python scripts/put_secrets.py --project <project-id> [--dry-run]

The secrets themselves are created empty by Terraform; this adds the first version of each. Values are
piped to `gcloud` on standard input - never put on a command line, printed, or written to a file. Only
secret NAMES are printed. Re-running adds a new version; old versions stay until you destroy them
(Secret Manager's free tier covers 6 active versions, so destroy superseded ones).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

SECRETS = ["DATABRICKS_TOKEN", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SUPABASE_DB_URL", "ANTHROPIC_API_KEY"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parent.parent / ".env"))
    parser.add_argument("--dry-run", action="store_true", help="show what would be uploaded, upload nothing")
    args = parser.parse_args()

    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        print("gcloud was not found on PATH.")
        return 1
    values = dotenv_values(args.env_file)

    failed = 0
    for name in SECRETS:
        value = values.get(name) or ""
        if not value.strip():
            print(f"  skip   {name}: not set in {args.env_file}")
            continue
        if args.dry_run:
            print(f"  would add a new version of {name} ({len(value)} characters)")
            continue
        result = subprocess.run(
            [gcloud, "secrets", "versions", "add", name, "--data-file=-", f"--project={args.project}"],
            input=value.encode("utf-8"), capture_output=True, check=False,
        )
        if result.returncode == 0:
            print(f"  added  {name}")
        else:
            failed += 1
            print(f"  FAILED {name}: {result.stderr.decode(errors='replace').strip().splitlines()[-1:]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
