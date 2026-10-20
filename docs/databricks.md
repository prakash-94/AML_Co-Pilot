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
   It stores `SF_PASSWORD`, `SF_SECURITY_TOKEN` and `SUPABASE_DB_URL`. There is no model API key (see "Gemini on Vertex AI"). The Supabase address is
   converted to the IPv4 *pooler* (the direct host is IPv6-only and unreachable from Databricks serverless).
   `DATABRICKS_CLIENT_SECRET` (the service principal) is already in the scope.
3. The service tables (`documents`, `audit_objects`, `reviewers`) are created by the first job run. Add reviewers:
   ```sql
   INSERT INTO workspace.aml_copilot.reviewers VALUES ('you@example.com', 'supervisor');   -- or 'investigator'
   ```

## Gemini on Vertex AI (keyless)
The models are Google Gemini on Vertex AI: **Gemini 3.1 Pro** (preview) judges typologies, writes the narrative and
fact-checks it; **Gemini 3.5 Flash** summarizes the evidence. Organisation policy forbids service-account key files,
and Vertex API keys are not accepted for a normal project, so the jobs log in to Google without a stored secret:

1. The job asks Databricks for an OAuth token for its service principal (`aml-copilot-app`).
2. Google's Security Token Service swaps it for a Google token, because a workload identity pool trusts this workspace
   (`https://<host>/oidc`) for exactly that one principal.
3. The token impersonates the Google service account `aml-gemini`, which holds only the *Vertex AI User* role.

One-time Google setup (already done for this project; `<NUM>` is the project number):
```bash
gcloud iam service-accounts create aml-gemini --project <PROJECT>
gcloud projects add-iam-policy-binding <PROJECT> --member=serviceAccount:aml-gemini@<PROJECT>.iam.gserviceaccount.com --role=roles/aiplatform.user
gcloud iam workload-identity-pools create databricks --location=global --project <PROJECT>
gcloud iam workload-identity-pools providers create-oidc databricks --workload-identity-pool=databricks --location=global --project <PROJECT>   --issuer-uri="https://<HOST>/oidc" --allowed-audiences=<WORKSPACE_ID> --attribute-mapping="google.subject=assertion.sub"   --attribute-condition="assertion.sub == '<SERVICE_PRINCIPAL_APPLICATION_ID>'"
gcloud iam service-accounts add-iam-policy-binding aml-gemini@<PROJECT>.iam.gserviceaccount.com --role=roles/iam.workloadIdentityUser   --member="principal://iam.googleapis.com/projects/<NUM>/locations/global/workloadIdentityPools/databricks/subject/<SERVICE_PRINCIPAL_APPLICATION_ID>"
```
The three non-secret values the bundle needs (`GCP_PROJECT`, `GCP_WIF_AUDIENCE`, `GCP_SERVICE_ACCOUNT`) live in `.env`
for local deploys and in GitHub repository variables for CI. To run the pipeline from a laptop, set `GCP_AUTH=token`
and `GCP_ACCESS_TOKEN=$(gcloud auth print-access-token)`.

Gemini bills its hidden "thinking" tokens as output, so the cost meter counts them. Gemini 3.1 Pro is a *preview*
model: Google can change or retire it with notice.

## Deploy and run
```bash
python scripts/dbx_deploy.py validate
python scripts/dbx_deploy.py deploy                                   # builds the wheel, uploads, creates jobs + pipeline
python scripts/dbx_deploy.py run investigate --params account_id=ACC-H-00000
python scripts/dbx_deploy.py run canary
```
You can also press **Run now with different parameters** on the `aml-investigate` job in the Databricks UI and type an
`account_id`.

## The web page
The page is a Databricks App named `aml-copilot` (https://aml-copilot-7474648796792570.aws.databricksapps.com). Open it
while signed in to Databricks. Type an account id and press **Investigate** (it asks Databricks to run the
`aml-investigate` job as you), open any investigation to read the draft, fact check and audit trail, and approve, escalate
or reject it.

**Gemini or Claude, and comparing them.** The **Model** drop-down chooses Gemini (Vertex AI), Claude (Anthropic API, Haiku 4.5
summarizes and Sonnet 5 judges and writes) or **Both, then compare**. Both runs queue and run one after the other (the job
allows one at a time), then the page opens the two drafts side by side with a table of the differences (typologies
matched, sentences supported, fact check, warnings, time) highlighted. You can also tick any two investigations in the list
and press **Compare selected**. Claude runs need `ANTHROPIC_API_KEY` in the secret scope (`python scripts/put_databricks_secrets.py`);
without it a Claude run fails with a clear message and Gemini runs are unaffected. Each run is recorded with its model family.

Only people in the `reviewers` table can use it; only a supervisor can approve.

* Deploy or redeploy: `python scripts/dbx_deploy.py deploy` then `python scripts/dbx_deploy.py run copilot` (starts it).
* First time only: `python scripts/grant_app_access.py` gives the app's own identity access to the service tables.
* Try changes locally first: `python scripts/run_local_app.py` (opens http://localhost:8000 against the real workspace).
* Limits on Free Edition: at most 3 apps, and an app stops after about 24 hours of running. If the page says it is
  unavailable, run `python scripts/dbx_deploy.py run copilot` to start it again.
* The page gets the shared code from `scripts/build_app.py`, which copies `aml_copilot/` next to the app's `main.py`
  during every deploy.

## Reviewing a draft (notebook)
Open `Workspace > Users > you > .bundle > aml_copilot > prod > files > databricks > notebooks > review`, pick an
**action** (`list`, `show`, `approve`, `reject`, `escalate`), paste the **investigation id**, write a **comment**, and run
all cells. The notebook records you (your Databricks login) as the reviewer. `show` also verifies the audit chain.

### Reviewing without the browser
If the Databricks web page keeps signing you out, review from a terminal instead. It runs the same logic as the notebook
and records you (the owner of your `DATABRICKS_TOKEN`) as the reviewer:
```bash
python scripts/dbx_review.py list
python scripts/dbx_review.py show <investigation id>
python scripts/dbx_review.py approve <investigation id> --comment "Reviewed the cited transactions"
```

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
`DATABRICKS_CLIENT_ID`, `SF_USERNAME`, `GCP_PROJECT`, `GCP_WIF_AUDIENCE`, `GCP_SERVICE_ACCOUNT`.
