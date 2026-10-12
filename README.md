# AML Investigation Copilot

A multi-agent system that investigates a flagged bank account and drafts a Suspicious Activity Report (SAR)
narrative **for a human to review**. It never files anything and never decides anything: every draft is
fact-checked sentence by sentence, every action is written to a tamper-evident audit trail, and approving a
draft needs a supervisor.

All data is synthetic (100K transactions, 2,100 customer profiles, 47K case records). This is a portfolio
project, not a compliance product.

## How an investigation works

```
 account id ──► Evidence Agent ──► Typology / RAG Agent ──► Narrative Agent ──► Faithfulness check ──► human review
                 │ 3 live sources     │ matches the evidence    │ drafts the SAR      │ Claude must QUOTE the
                 │ rule-based risk    │ to written typologies   │ narrative from the  │ evidence for every
                 │ score (no ML)      │ (FinCEN / FATF docs)    │ evidence, nothing   │ sentence; code checks
                 ▼                    ▼                         ▼ else                ▼ the quote exists
        Databricks · Salesforce · Supabase     ChromaDB + local MiniLM embeddings
```

* **Evidence**: bounded queries (never `SELECT *`; the ground-truth label column is never read), network
  expansion across counterparty accounts, and two rule-based detectors (structuring, layering) that produce a
  0–100 risk score. A source outage is reported as *incomplete*, never as "all clear".
* **Typology (RAG)**: retrieves red flags from the typology documents; code (not the model) drops any match
  that cites evidence the system never retrieved.
* **Narrative**: follows FinCEN's narrative guidance; code drops any sentence containing an amount, date or id
  found nowhere in the evidence.
* **Faithfulness**: a second model call fact-checks every sentence and must quote the supporting evidence;
  plain code verifies the quote really exists.
* Models: Claude Haiku 4.5 summarises evidence; Claude Sonnet 5 judges typologies, writes and fact-checks.

## Measured results (21 accounts, saved in `evaluation_results/phase5_report.md`)

| Measure | Result |
|---|---|
| Risk tier correct | 100% |
| Typology precision / recall | 100% / 100% |
| False alarms on normal accounts | 0% |
| Draft sentences supported by evidence | 99% of 236 |
| Drafts passing the fact check outright | 80% (the rest are flagged for a human) |
| Planted errors caught by the Sonnet fact-checker | 68 of 68 (3% false alarms; Haiku flagged 35% of true sentences) |
| Cost per investigation | about 2–6 US cents |

Not measured: behaviour on real bank data, agreement with human investigators, latency under load. The sample
is small and synthetic; treat these as regression baselines, not accuracy claims.

## The deployed system (Databricks)

Everything runs inside one Databricks workspace as an Asset Bundle (`databricks.yml`). There is no web server.

```
 You ── Run now (account_id) ──► Job "aml-investigate"  (serverless Python, one run at a time)
                                   │ reads   Unity Catalog view transactions_safe (no label column), Salesforce, Supabase
                                   │ calls   Claude through the Anthropic API (keys in a Databricks secret scope)
                                   │ writes  Delta: documents (job + draft), audit_objects (append-only, hash-chained)
 You ── open notebook ─────────► "review": list / show / approve / reject / escalate   (runs AS the reviewer)
 Schedule (Mon 06:00 UTC) ─────► Job "aml-canary" ─► 7-account quality check ─► Delta eval_runs ─► fails on regression
 Schedule (daily 07:00 UTC) ───► Job "aml-refresh-reports" ─► Lakeflow pipeline "aml-reporting" (materialized views)
```

* **Jobs, not servers.** One investigation is one job run. The job validates the account id, applies the daily limit
  and the one-open-run-per-account rule, runs the four agents, stores the draft (never the raw rows) and writes the
  audit trail. A crash is recorded on the job and the run fails visibly.
* **State in Delta.** `documents` holds job records with versioned, optimistic writes (`MERGE` that only succeeds if the
  version still matches). `audit_objects` is created with `delta.appendOnly = true`, so Delta itself refuses UPDATE and
  DELETE; each event carries the hash of the one before it.
* **Human decision.** The review notebook runs as the person who opens it, so the reviewer's identity is their real
  Databricks login, not a typed name. Roles come from the `reviewers` table; only a supervisor can approve. A decision
  is final and is tied to the exact draft by its SHA-256, so approving a draft that changed is refused.
* **Data governance.** Jobs read transactions as a service principal that can `SELECT` only the `transactions_safe`
  view. The ground-truth label and the base table are refused (tested live).
* **Reporting pipeline.** A Lakeflow declarative pipeline builds `investigations_flat`, `investigations_daily`,
  `canary_trend` and `audit_chain_links` (a SQL check that every audit event points at the previous one).
* **Quality gate.** The weekly canary fails the job if a tier or typology is wrong or fewer than 90% of draft
  sentences are supported.

Deploy and operate: [`docs/databricks.md`](docs/databricks.md). An earlier Google Cloud Run deployment of the same
service rules is described in [`docs/deploy.md`](docs/deploy.md) and is being retired.

## Running it locally

```bash
py -3.11 -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt           # copy .env.example to .env and fill it in first
python -m aml_copilot.agents.investigation ACC-H-00000          # one investigation on the command line
uvicorn aml_copilot.service.app:app --port 8080                  # optional: the HTTP API with in-memory storage + dev tokens
pytest -q                                                       # 550 tests, no network or API keys needed
```

Dev tokens `dev-investigator` / `dev-supervisor` work only locally; the app refuses dev authentication when it
detects Cloud Run.

## Layout

| Path | What |
|---|---|
| `aml_copilot/ingestion`, `detection` | sources, retries, entity resolution, network expansion, rule detectors |
| `aml_copilot/agents` | evidence, typology, narrative agents and the LangGraph orchestrator |
| `aml_copilot/retrieval`, `typology_docs/` | chunking, embeddings, vector store; the typology corpus |
| `aml_copilot/guardrails`, `evaluation` | faithfulness + consistency checks; the evaluation harness and canary |
| `aml_copilot/service` | job rules, audit chain, Delta adapters (+ an optional HTTP API and Google Cloud adapters) |
| `aml_copilot/dbx` | the Databricks job entry points, review logic, secret loading |
| `databricks.yml`, `databricks/` | the bundle: jobs, reporting pipeline (SQL), review notebook |
| `scripts/` | deploy and secret helpers; `infra/`, `web/`, `Dockerfile` are the retiring Cloud Run deployment |

## Known limits

* Only two typologies (structuring, layering) have rule-based detectors; the other five are retrieved by
  similarity only and are marked "no detector yet".
* Databricks Free Edition has no Claude models, so Claude is called through the Anthropic API from the job. Claude on
  Vertex AI is implemented and tested offline, but Google has not granted this project quota.
* The workspace has one user, so the roles are demonstrated with a `reviewers` table rather than several logins.
* The service principal's OAuth secret expires after 90 days and must be rotated.
* Each investigation job installs its libraries on start-up, so a run takes several minutes before the first query.
* Free Edition may delete an inactive workspace; the weekly canary doubles as a heartbeat.
