"""Review investigations from a terminal - no browser or Databricks login needed.

    python scripts/dbx_review.py list
    python scripts/dbx_review.py show <investigation id>
    python scripts/dbx_review.py approve <investigation id> --comment "Reviewed the cited transactions"
    python scripts/dbx_review.py reject|escalate <investigation id> --comment "..."

It runs the same review logic as the Databricks notebook. The reviewer is whoever owns the DATABRICKS_TOKEN in your
.env (Databricks reports it as `current_user()`), and the role comes from the `reviewers` table - so approving still
needs a supervisor, and the decision is recorded in the audit trail under your name.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aml_copilot.config import Settings
from aml_copilot.dbx.review import ACTIONS, run_action
from aml_copilot.dbx.runtime import DEFAULT_SCHEMA, delta_services
from aml_copilot.errors import AMLCopilotError
from aml_copilot.ingestion.sources import DatabricksSource
from aml_copilot.service.delta import connector_sql


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("investigation_id", nargs="?", default="")
    parser.add_argument("--comment", default="")
    args = parser.parse_args()

    # Sign in with your personal token (not the service principal), so the recorded reviewer is you.
    settings = Settings(databricks_client_id="", databricks_client_secret="")
    sql = connector_sql(DatabricksSource(settings)._open)
    store, audit, roles = delta_services(sql, DEFAULT_SCHEMA)
    user = sql("SELECT current_user() AS u", {})[0]["u"]
    try:
        print(run_action(args.action, args.investigation_id, args.comment, user, datetime.now(UTC),
                         store=store, audit=audit, roles=roles))
    except (AMLCopilotError, ValueError) as error:
        print(f"NOT DONE: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
