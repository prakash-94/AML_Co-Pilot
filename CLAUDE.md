# CLAUDE.md - AML Investigation Copilot

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Facts in this file and in DECISIONS.md and FLOW.md were checked against the code, tests, docs, README and git history on 2026-11-19. Where the repository does not say why something was chosen, the text says UNKNOWN. Never replace UNKNOWN with a guess; ask the owner.

## What this project is (plain English)
A tool for bank investigators. Someone gives it one flagged account number, and it gathers the facts about that account, checks them against written descriptions of known money-laundering patterns, and drafts the text of a Suspicious Activity Report (SAR) for a person to review. A SAR is the report a bank files with the regulator when it sees suspicious activity.

It never files a report and never decides anything: a person reads the draft and approves, rejects or escalates it, and only a supervisor can approve. All data is synthetic, and this is a portfolio project, not a compliance product.

## One-line architecture
Account id -> Evidence (3 data systems + rule-based risk score) -> Typology match (RAG: look up red flags in documents, then a model judges) -> Narrative (model drafts, code checks) -> Fact check (model must quote proof, code verifies) -> Human reviewer (approve / reject / escalate)

## Tech stack (one line of why each)
- Python 3.11: the project requires 3.11 or newer (`pyproject.toml`). Why 3.11 and not newer: UNKNOWN.
- LangGraph: each agent, and the chain of agents, is a graph of steps that can stop early on an error (`agents/investigation.py`). Why LangGraph rather than another tool: UNKNOWN.
- Pydantic: describes and checks the shape of every model answer and every stored record (`agents/models.py`, `service/models.py`). Why Pydantic: UNKNOWN.
- ChromaDB + sentence-transformers (`all-MiniLM-L6-v2`): stores and searches the typology documents on the job's own machine with no API key and no per-call cost (D-007).
- Google Gemini on Vertex AI (3.5 Flash summarizes, 3.1 Pro judges, writes and fact-checks) and Anthropic Claude (Haiku 4.5, Sonnet 5), chosen per run (D-010, D-021, D-026).
- Databricks (Delta tables, jobs, Lakeflow pipeline, Asset Bundle, Apps): where everything runs (D-017).
- Salesforce (`simple-salesforce`), Supabase Postgres (`psycopg2`), Databricks SQL (`databricks-sql-connector`): the three data sources. Why these three systems: UNKNOWN.
- FastAPI: serves the review web page (`dbx/webapp.py`). Why FastAPI: UNKNOWN.
- tenacity: retries only errors known to be temporary (`ingestion/retry.py`).
- structlog: structured logging (`logging_config.py`).
- pytest, ruff, gitleaks, GitHub Actions: tests, linting, secret scan, automated checks on every push (`.github/workflows/ci.yml`).

## Folder map
- `aml_copilot/ingestion/` fetches the three data sources, merges and cleans them, and follows money to linked accounts.
- `aml_copilot/detection/` the two rule detectors (structuring, layering) and the 0-100 risk score. No model is used here.
- `aml_copilot/agents/` the evidence, typology and narrative agents, the chain that runs them (`investigation.py`), and the model-client factory (`llm.py`).
- `aml_copilot/retrieval/` cuts the typology documents into pieces, stores them in ChromaDB and searches them.
- `aml_copilot/guardrails/` the fact check (`faithfulness.py`) and free exact checks (`consistency.py`).
- `aml_copilot/evaluation/` measures the pipeline against known right answers and runs the weekly health check (the "canary").
- `aml_copilot/service/` the rules around a job (run once, daily limit, final decisions), the audit log, and the Delta table storage.
- `aml_copilot/dbx/` the Databricks glue: job entry points, review logic, the web page's server and page files (`static/`).
- `typology_docs/` the written knowledge base: 7 laundering patterns and the SAR writing standard.
- `databricks/` the job scripts, review notebook, reporting SQL and web-app launcher; `databricks.yml` defines everything deployed.
- `scripts/` helper commands: deploy, copy secrets, review from a terminal, run the page locally.
- `tests/` one test file per area, using fakes instead of real systems.
- `docs/` `databricks.md` (set-up and operation). `phase0-checklist.md` is a leftover from the Google Cloud setup.
- `evaluation_results/`, `sample_runs/` saved measurement reports and example run outputs.
- Root: `README.md`, `seed_high_risk_accounts.py`, `generate_salesforce_customers.py`, `customer_profiles*.csv`, `.env.example`.

## How to run it
- Install: `py -3.11 -m venv .venv`, activate it, then `pip install -r requirements.txt`. Copy `.env.example` to `.env` and fill it in.
- Run one investigation: `python -m aml_copilot.agents.investigation ACC-H-00000` (needs `.env`).
- Test: `pytest -q` (no network, no keys needed).
- Lint: `ruff check aml_copilot tests seed_high_risk_accounts.py` (the same command the CI uses).
- Rebuild the document index: `python -m aml_copilot.retrieval build`.
- Measure quality (spends money unless you omit `--yes`): `python -m aml_copilot.evaluation run --yes --cap 3`.
- Deploy to Databricks: `python scripts/dbx_deploy.py validate` then `deploy`; start the page with `run copilot`; run one account with `run investigate --params account_id=ACC-H-00000`.
- Try the page on your own machine: `python scripts/run_local_app.py`, then open http://localhost:8000.
- Review without the browser: `python scripts/dbx_review.py list | show <id> | approve <id> --comment "..."`.
- Accounts to try: `ACC-H-00000` (both patterns, high), `ACC-L-*` (layering), `ACC-S-*` (structuring), `ACC-N-*` (normal, ends as no action).

## Hard rules (never break)
- Never put passwords, keys or tokens in any file or chat. Never print or open the values in `.env`.
- Never read or show the hidden `ground_truth_label` column to any agent (D-005).
- Never let a missing data source look like "all clear": it must end as `incomplete` (D-006).
- Never trust a model's output on its own: code must check it, and a person reviews every draft (D-002).
- Never store or return raw transaction rows in a result; `InvestigationResult` has no field for them (`service/models.py`).
- Never file or send anything to a regulator; an approval only means "approved as a draft for manual filing".
- Validate every account id with `validate_account_id` before it reaches SQL or SOQL (`ids.py`).
- Edit code only in `aml_copilot/`. `databricks/app/aml_copilot/` is a build copy, ignored by git (`scripts/build_app.py`).
- Databricks job scripts must not call `sys.exit(0)`: Databricks reports it as a failure (C-010).
- Do not state a measured number you did not measure. Write "Not measured yet".

## Testing rules
- Every new feature or bug fix comes with tests, written in the same task.
- Before any git push, run the FULL test suite. If anything fails, fix it first. Never push with failing tests.
- After each run, tell me the total number of tests and how many are new. The count only goes up. If a test is removed, explain why in DECISIONS.md.
- Tests must not need the internet, real keys or real data. Use fakes.
- Test command: `pytest -q`
- Baseline on 2026-11-19 (commit 50c928c): 577 tests, all passing in about 40 seconds; `ruff check` clean. This is the number the count must not go below.
- A test that touches settings must not leak into the real environment: `tests/test_dbx.py` has an automatic fixture, `private_environment`, for that.
- To run one test: `pytest tests/test_webapp.py::test_a_request_with_no_identity_is_401 -q`.

## Code style rules
- Comments explain WHY, not what. No comment that repeats the code.
- Every function has a short docstring: purpose, inputs, output, errors.
- Link non-obvious choices to the decision, for example "# See D-004".
- No commented-out code, no stray prints, no unexplained magic numbers.
- Small functions with clear names. Run the linter before every commit (`ruff check ...`, above). Whether a code formatter is also meant to be run: UNKNOWN (none is configured).
- Line length is 100 characters (`pyproject.toml`).

## Memory files
- DECISIONS.md = every choice, the options rejected, and the challenges
- FLOW.md = the whole flow, which file/function runs when, and why
- README.md = the public summary; docs/databricks.md = how to deploy and operate

## Rules for Claude
1. For anything non-trivial, show a short plan first and wait for my approval.
2. After every feature or fix, update DECISIONS.md and FLOW.md in the same task.
3. Add a DECISIONS.md entry whenever a choice is made: options considered, what we picked, why, and the trade-off. Add a Challenge entry for every bug or blocker that took real effort.
4. Keep FLOW.md in sync with the code: steps, file names, diagram.
5. Explain in plain English. I am not a programmer. Define any technical word the first time you use it.
6. If you do not know why a past choice was made, write UNKNOWN. Never invent a reason.
