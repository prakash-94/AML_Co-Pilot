# Running the copilot on Databricks

Everything is defined in `databricks.yml` and deployed as one Asset Bundle. Commands run from `E:\AML`; the helper
`scripts/dbx_deploy.py` reads your `.env` (host, token, warehouse path, Salesforce user) and passes it to the Databricks
CLI, so nothing secret is typed or printed.

## One-time setup
1. Install the CLI: `winget install Databricks.DatabricksCLI`.
2. Put the credentials in the Databricks secret scope `aml-copilot` (you run this; values are never printed):
   ```bash
   python scripts/put_databricks_secrets.py --dry-run     # shows names and lengths
   python scripts/put_databricks_secrets.py
   ```
   It stores `ANTHROPIC_API_KEY`, `SF_PASSWORD`, `SF_SECURITY_TOKEN` and `SUPABASE_DB_URL`. The Supabase address is
   converted to the IPv4 *pooler* (the direct host is IPv6-only and unreachable from Databricks serverless).
   `DATABRICKS_CLIENT_SECRET` (the service principal) is already in the scope.
3. The service tables (`documents`, `audit_objects`, `reviewers`) are created by the first job run. Add reviewers:
   ```sql
   INSERT INTO workspace.aml_copilot.reviewers VALUES ('you@example.com', 'supervisor');   -- or 'investigator'
   ```

## Deploy and run
```bash
python scripts/dbx_deploy.py validate
python scripts/dbx_deploy.py deploy                                   # builds the wheel, uploads, creates jobs + pipeline
python scripts/dbx_deploy.py run investigate --params account_id=ACC-H-00000
python scripts/dbx_deploy.py run canary
```
You can also press **Run now with different parameters** on the `aml-investigate` job in the Databricks UI and type an
`account_id`.

## Reviewing a draft
Open `Workspace > Users > you > .bundle > aml_copilot > prod > files > databricks > notebooks > review`, pick an
**action** (`list`, `show`, `approve`, `reject`, `escalate`), paste the **investigation id**, write a **comment**, and run
all cells. The notebook records you (your Databricks login) as the reviewer. `show` also verifies the audit chain.

## What each piece does
| Piece | Where |
|---|---|
| Investigate job | `databricks/jobs/investigate.py` -> `aml_copilot/dbx/jobs.py` -> `dbx/investigate.py` |
| Review logic | `aml_copilot/dbx/review.py` (the notebook only calls it) |
| State and audit | `aml_copilot/service/delta.py`: Delta `documents` and append-only `audit_objects` |
| Canary | `aml_copilot/evaluation` (`canary`), table `eval_runs` |
| Reporting pipeline | `databricks/pipelines/reporting.sql` |
| Settings | job parameters (`--set KEY=VALUE`) for non-secrets; the secret scope for secrets |

## Operations
* Jobs allow **one investigate run at a time** (`max_concurrent_runs: 1`, queued). The Delta document store relies on
  this: its writes check document versions and refuse a conflicting change, but are not designed for parallel runs.
* Rotate the service principal's OAuth secret every 90 days (it is read from the scope as `DATABRICKS_CLIENT_SECRET`).
* If Free Edition shuts compute down for the day, runs fail with a clear error; nothing is reported as "all clear".

## CI/CD
`.github/workflows/deploy.yml` (manual, approval-gated) validates, deploys the bundle and can run a smoke investigation.
It needs the secrets `DATABRICKS_HOST`, `DATABRICKS_TOKEN` and the variables `DATABRICKS_SERVER_HOSTNAME` (bare host), `DATABRICKS_WAREHOUSE_HTTP_PATH`,
`DATABRICKS_CLIENT_ID`, `SF_USERNAME`.
