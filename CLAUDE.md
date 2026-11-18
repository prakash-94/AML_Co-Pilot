# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AML Investigation Copilot: given one flagged account id, a LangGraph pipeline (evidence -> typology/RAG -> narrative -> faithfulness check) drafts a SAR narrative **for a human to review**. It never files or decides anything. All data is synthetic. The deployed system is Databricks jobs plus one Databricks App web page; `README.md` and `docs/databricks.md` describe it in full. `FLOW.md` walks one investigation file by file; `DECISIONS.md` records why each choice was made, what was rejected and what broke.

## Commands

Windows, Python 3.11 in `.venv` (3.14 is too new for torch).

```bash
pip install -r requirements.txt          # full set incl. torch/sentence-transformers; requirements-ci.txt is what CI uses
pytest -q                                # ~577 tests, no network, no API keys
pytest tests/test_webapp.py::test_name -q   # a single test
ruff check aml_copilot tests seed_high_risk_accounts.py   # CI lints exactly this; line length 100

python -m aml_copilot.agents.investigation ACC-H-00000    # one live investigation (needs .env)
python -m aml_copilot.retrieval build                      # rebuild the ChromaDB index from typology_docs/
python -m aml_copilot.evaluation run --yes --cap 3         # spends money; without --yes it only prints the plan and estimate
python -m aml_copilot.evaluation canary --yes              # the 7-account health check

python scripts/dbx_deploy.py validate | deploy             # Databricks bundle; reads .env, prints nothing secret
python scripts/dbx_deploy.py run investigate --params account_id=ACC-H-00000
python scripts/dbx_deploy.py run copilot                   # (re)start the web app; Free Edition stops apps after ~24 h
python scripts/run_local_app.py                            # the web page on http://localhost:8000 against the real workspace
python scripts/dbx_review.py list | show <id> | approve <id> --comment "..."   # review without the browser
```

Accounts to try: `ACC-H-00000` (high, both patterns), `ACC-L-*` (layering), `ACC-S-*` (structuring), `ACC-N-*` (normal, should give `no_action`). Id families are `[SLNH]`, validated in `aml_copilot/ids.py` before any id reaches SQL or SOQL.

## Architecture (the parts that span many files)

**Pipeline.** `agents/investigation.py` chains `evidence -> typology -> narrative -> faithfulness -> conclude`; the first error skips to `conclude`. Each agent is its own small LangGraph graph in `agents/`. Run status is `draft_ready | no_action | incomplete | failed`. **Core rule (`decide_outcome`):** a source outage with nothing drafted is `incomplete`, never `no_action`; an outage must not read as all-clear.

**Model writes, code checks.** Every LLM output is validated in plain Python and never trusted to police itself: typology matches citing evidence that was not retrieved are dropped, `detector_corroborated` is set by code, narrative sentences with an amount/date/id absent from the evidence are dropped, and the fact-check must *quote* evidence which code verifies exists. Keep this pattern when adding steps. All LLM calls go through `agents/llm.py` (`make_chat_model`, `invoke_structured`, which retries one schema slip). Schemas have an LLM-facing form and a checked, final form (`agents/models.py`).

**Detection is rules, not ML.** `detection/structuring.py` (>=3 cash deposits of $7,000-$10,000 in a 90-day window), `layering.py` (wire chain <=72 h, shrinking amounts, cross-border), `risk_score.py` (50 points each; high >=70). Only these two typologies have detectors; the other five typologies in `typology_docs/` are retrieved by similarity only. `ingestion/expansion.py` walks counterparties (4 hops, 30 accounts, 500 rows, wires only) because layering chains are invisible to a per-account query; hitting a cap warns and marks evidence incomplete.

**Three sources, one join key.** Databricks Delta transactions, Salesforce `Customer_Profile__c`, Supabase `case_history`, joined on `account_id` (`ingestion/`). Queries are bounded, never `SELECT *`, and **`ground_truth_label` must never be read or shown to an agent**; jobs read the Unity Catalog view `transactions_safe`, which omits it. A source failure goes into `missing_sources`, it is not raised.

**Providers.** `LLM_PROVIDER` is `gemini` (default; Gemini 3.5 Flash summarizes, 3.1 Pro judges/writes/fact-checks, on Vertex AI), `anthropic` (Claude via API) or `vertex`. On Databricks the page's per-run `provider` job parameter (`dbx/providers.py`, `gemini|claude`) overrides settings. Gemini is reached with keyless workload identity federation (`agents/gcp_auth.py`; `GCP_AUTH` = `adc|gcloud|token|databricks`; use `gcloud` locally). Gemini quirks already handled: no free-form map schemas (citations are a list of `{claim, ids}` converted back, with a derive-from-claims fallback), hidden thinking tokens need output headroom, every call has a 300 s timeout and 2 retries.

**Service layer** (`service/`, storage-agnostic): job rules (claim-once, 30/day limit, one open run per account, decide-once bound to the result's SHA-256), a hash-chained append-only audit log, optimistic versioned writes. `service/delta.py` implements them on Delta (`documents`, `audit_objects` with `delta.appendOnly`, `reviewers` roles; only a supervisor approves). Stored results never contain the raw `bundle` rows. The Delta stores assume **one investigate run at a time** (`max_concurrent_runs: 1`).

**Databricks glue** (`dbx/`): `jobs.py`/`investigate.py` are the job entry points (`databricks/jobs/*.py` are thin wrappers); `review.py` holds approve/reject logic used by the notebook, terminal tool and web page alike; `webapp.py` is the FastAPI app (identity from `X-Forwarded-Email`, role from `reviewers`, state-changing routes require `X-Requested-With: aml-copilot`); `jobs_api.py` starts runs through the Jobs API; `static/` is the plain HTML/JS page. Everything is defined in `databricks.yml`: jobs `aml-investigate`, `aml-canary`, `aml-refresh-reports`, the `aml-reporting` Lakeflow pipeline (`databricks/pipelines/reporting.sql`) and the `aml-copilot` app.

## Gotchas

- Jobs run a **built wheel** (`python -m build --wheel`, done by the bundle); the app gets a **copy** of `aml_copilot/` at `databricks/app/aml_copilot/` made by `scripts/build_app.py` (git-ignored). Edit `aml_copilot/`, never the copy. Redeploy both after code changes.
- Job scripts must not call `sys.exit(0)`: Databricks reports it as a failure. Bundle variables come from `.env` via `dbx_deploy.py` (`BUNDLE_VAR_*`); CI sets them from GitHub secrets/variables.
- The review notebook must run on Serverless compute and pins old `pydantic`/`pydantic-settings` in its `%pip` line because the runtime's `typing_extensions` shadows the newer one. Jobs also install their libraries on start, so a run takes minutes before the first query.
- Supabase's direct host is IPv6-only; Databricks serverless needs the IPv4 pooler DSN (`put_databricks_secrets.py` converts it).
- Databricks PATs do not authenticate to Apps; the deployed page can only be driven from a signed-in browser. Use `run_local_app.py` to test changes first.
- Secrets live in the Databricks scope `aml-copilot` (set only by `scripts/put_databricks_secrets.py`; a Claude run needs `ANTHROPIC_API_KEY` there). Never print or open `.env` values. The service principal's OAuth secret expires every 90 days.
- Tests use fakes for every external system. `tests/test_dbx.py` has an autouse `private_environment` fixture because earlier tests leaked into `os.environ`; follow that pattern when a test touches settings.
- `retry.py` retries only known-transient errors and never Salesforce login failures (lockout risk). Seeding extra accounts: `seed_high_risk_accounts.py` is dry-run by default (`--apply`, `--remove`).
- Gemini 3.1 Pro is a preview model; evaluation reports live in `evaluation_results/` and measured numbers must be real ("not measured yet" beats an invented figure).
