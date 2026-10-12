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

## The deployed system (Google Cloud)

```
 Browser ─ Firebase Hosting ─┬─ web/  (sign-in, submit, review, audit view)
                             └─ /api/** ─► Cloud Run "aml-api"  (checks the Firebase token on every call)
                                              │ job + quota + duplicate lock (Firestore transaction)
                                              │ audit event (Cloud Storage, write-once, hash-chained)
                                              ▼
                                         Cloud Tasks ─► Cloud Run "aml-worker" (private) ─► the pipeline
 Cloud Scheduler (weekly) ─► Cloud Run Job "aml-eval" ─► 7-account canary ─► Delta table eval_runs
```

* **Roles**: investigators can reject or escalate; only supervisors can approve. The role is a signed claim.
* **A job runs once** (Cloud Tasks delivers at least once; a transactional claim makes a duplicate harmless).
* **A decision is final** and bound to the exact draft by its SHA-256, so approving a stale draft is refused.
* **Audit trail**: each event includes the hash of the previous one; the bucket's retention policy blocks
  overwriting or deleting objects (tested). `GET /api/investigations/{id}/audit` verifies the chain.
* **Data governance**: the app reads Databricks as a service principal that can `SELECT` only a view
  (`transactions_safe`) with no label column; the base table and the label are refused (tested live).
* **Brakes on spend**: daily investigation limit, max 2 instances, one task attempt, per-run caps, $15 budget alert.
* **Alerts** (email): 5xx errors, queue backlog, crashed investigations, a failed weekly quality check.
* The quality check fails (and alerts) if a tier or typology is wrong or fewer than 90% of draft sentences are supported.

Deploy and operate: see [`docs/deploy.md`](docs/deploy.md). Infrastructure is Terraform in `infra/`.

## Running it locally

```bash
py -3.11 -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt           # copy .env.example to .env and fill it in first
python -m aml_copilot.agents.investigation ACC-H-00000          # one investigation on the command line
uvicorn aml_copilot.service.app:app --port 8080                  # the API with in-memory storage + dev tokens
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
| `aml_copilot/service` | jobs, audit chain, auth, API, worker, Google Cloud adapters |
| `web/`, `infra/`, `scripts/`, `Dockerfile`, `cloudbuild.yaml` | page, Terraform, helper scripts, image build |

## Known limits

* Only two typologies (structuring, layering) have rule-based detectors; the other five are retrieved by
  similarity only and are marked "no detector yet".
* Claude on Vertex AI is implemented and tested offline, but Google has not granted quota to this project, so the
  deployment uses the Anthropic API (`LLM_PROVIDER=vertex` switches it).
* The Databricks service principal's OAuth secret expires after 90 days and must be rotated.
* Audit retention is 7 days and unlocked on purpose (locking is irreversible); lock it for production.
