# FLOW.md: how one investigation works, start to finish

Use this for "walk me through one request", "where is the RAG?" and "where are the agents?". Every step names the file that does it. Numbers come from the code and from `README.md`; anything not measured is said so.

## The 30-second version

A reviewer types an account id on a web page. A Databricks job validates it, pulls evidence from three systems, scores it with rules, retrieves matching laundering patterns (RAG), drafts a SAR narrative, and fact-checks the draft sentence by sentence. The draft and an audit trail go into Delta tables. A human opens the draft and approves, rejects or escalates it. Nothing is ever filed automatically.

```
 reviewer ─► web page (Databricks App) ──POST /api/investigations──► Jobs API "run now"
                                                                       │
                                  Databricks job  aml-investigate  ◄───┘   (serverless, one run at a time)
                                    │
   validate id ─► daily limit + one open run per account ─► claim the job once ─► audit JOB_SUBMITTED / RUN_STARTED
                                    │
                    ┌───────────────┴────────────────────────────────────────────────┐
                    │  LangGraph:  evidence ─► typology (RAG) ─► narrative ─► fact check ─► conclude  │
                    └───────────────┬────────────────────────────────────────────────┘
                                    │
   store the draft (no raw rows) + SHA-256 ─► Delta `documents`;  audit RUN_FINISHED ─► Delta `audit_objects`
                                    │
 reviewer ◄── web page / notebook / terminal ── reads draft + audit ── approve · reject · escalate (supervisor to approve)
```

## Step by step

### 0. Starting a run
| What happens | File |
|---|---|
| The page posts an account id and a model choice (`gemini` or `claude`) | `dbx/static/app.js` |
| The API checks identity (`X-Forwarded-Email`), the role (`reviewers` table), the same-origin header, and that `provider` is valid | `dbx/webapp.py` |
| It calls the Jobs API `run-now` with job parameters `account_id` and `provider` | `dbx/jobs_api.py` |
| The job parameters become command-line arguments of the job script | `databricks.yml` -> `databricks/jobs/investigate.py` -> `dbx/jobs.py` (`investigate_main`) |

The web app never runs an investigation. It only starts the job and reads results.

### 1. Job set-up (`dbx/jobs.py`)
1. `apply_config` applies non-secret `--set KEY=VALUE` settings; a Claude run adds the Claude settings from `dbx/providers.py`.
2. `load_secrets` (`dbx/runtime.py`) copies credentials from the Databricks secret scope `aml-copilot` into the environment.
3. `create_tables_sql` (`service/delta.py`) creates `documents`, `audit_objects` (append-only) and `reviewers` if missing.
4. `build_index()` (`retrieval/store.py`) builds the ChromaDB vector index from `typology_docs/` inside the job.

### 2. Service rules before any model is paid for (`dbx/investigate.py`, `service/jobs.py`)
1. `validate_account_id` (`ids.py`) accepts only `ACC-[S|L|N|H]-nnnnn`, so nothing odd reaches SQL or SOQL.
2. `store.submit` enforces the daily limit (30) and one open run per account; a duplicate or over-limit request returns without running.
3. The audit log records `JOB_SUBMITTED`.
4. `process_job` (`service/worker.py`) claims the job exactly once, records `RUN_STARTED`, and runs the pipeline. A crash is recorded on the job and as `RUN_FAILED`; it is never retried, because a retry would be a second paid run.

### 3. The pipeline (`agents/investigation.py`, a LangGraph graph)
Order: `evidence -> typology -> narrative -> faithfulness -> conclude`. The first error skips straight to `conclude`. Each agent is its own small graph.

**3a. Evidence agent** (`agents/evidence.py`; nodes: ingest -> score -> summarize)
- *Ingest* (`ingestion/pipeline.py`): the three sources are queried concurrently (`ingestion/sources.py`). Queries are bounded, never `SELECT *`, and the label column is never read (jobs use the view `transactions_safe`). A failed source is recorded in `missing_sources`, not raised. Retries are in `ingestion/retry.py`.
- *Expand* (`ingestion/expansion.py`): follows counterparties both ways over wire transfers, up to 4 hops, 30 accounts and 500 rows. A cap hit produces a warning and marks the evidence incomplete.
- *Normalize* (`ingestion/normalize.py`): resolves ids and field formats; `merge_sources` joins on `account_id`.
- *Score* (`detection/`): `structuring.py` and `layering.py` are rule detectors; `risk_score.py` combines them (50 points each, high from 70, medium from 30). No model is involved.
- *Summarize*: one model call (Flash, or Haiku on a Claude run) writes a factual, cited `EvidenceSummary`. It reports the score as an attributed fact and adds no suspicion judgment of its own.

**3b. Typology agent, the RAG step** (`agents/typology.py`; nodes: retrieve -> match)
- *Retrieve*: `build_queries` makes one query per evidence field; `retrieval/store.search` finds the nearest passages in ChromaDB (embeddings from `all-MiniLM-L6-v2`, computed locally). The corpus is `typology_docs/`, split by heading in `retrieval/chunking.py`. Only passages tagged `typology` are searched.
- *Match*: a model judges which typologies the evidence supports. `finalize_assessment` then checks the result in plain code: it drops matches that cite a document that was not retrieved or evidence ids that do not exist, ranks the rest, and sets `detector_corroborated` itself (the model never sees that field).
- Retrieval distance cannot decide a match on its own, so `max_distance` (0.75) is only a noise filter.

**3c. Narrative agent** (`agents/narrative.py`; nodes: prepare -> write)
- `should_draft` gates it: no draft if the tier is low and nothing matched, so a clean account ends as `no_action` without calling the model.
- `retrieval/questions.py` reads the investigator questions straight from the typology files; the writing prompt follows `typology_docs/sar_narrative_standards.md` (FinCEN's who, what, when, where, why, how).
- `finalize_narrative` drops any sentence containing an amount, date or id that appears nowhere in the evidence, strips unknown ids, and warns on verdict-like wording.

**3d. Faithfulness check** (`guardrails/faithfulness.py`, `guardrails/consistency.py`)
- A second model call judges every sentence and must **quote** the supporting evidence. `quote_in_evidence` verifies in code that the quote really exists; otherwise the verdict is "unverified".
- `check_consistency` runs free deterministic checks (score, tier, prior-case count, typology named).
- This step does not stop a draft: flagged sentences are listed for the human, never auto-edited.

**3e. Conclude** (`decide_outcome` in `agents/investigation.py`)
Status is one of `draft_ready`, `no_action`, `incomplete` or `failed`. **If any source was down and nothing was drafted, the status is `incomplete`, never `no_action`**, so an outage cannot read as "all clear".

All model calls go through `agents/llm.py` (`make_chat_model`, `invoke_structured`). Gemini is reached with a keyless Google login (`agents/gcp_auth.py`).

### 4. Storing the result (`service/worker.py`, `service/models.py`, `service/delta.py`)
- `to_api_result` turns the run state into the stored result: the draft, checks, warnings, timings and model provenance, **without the raw transaction rows**, plus a SHA-256 `result_hash`.
- `store.complete` writes it to Delta `documents` with a versioned `MERGE` (it only succeeds if the version still matches).
- `RUN_FINISHED` is appended to `audit_objects`. Each event carries the hash of the previous one, and Delta refuses UPDATE and DELETE on that table.

### 5. Human review (`dbx/review.py`, `dbx/webapp.py`)
- The page lists investigations (`GET /api/investigations`), opens one with its audit trail, and can compare two drafts side by side.
- `POST .../decision` calls `review.decide`: the person's real Databricks login is the reviewer, only a supervisor can approve, a decision is final, and it is bound to the draft's SHA-256, so approving a draft that changed is refused (`StaleDraftError`).
- The same logic backs the notebook (`databricks/notebooks/review.py`) and the terminal tool (`scripts/dbx_review.py`).

### 6. Reporting and quality (outside the request path)
- `aml-reporting` pipeline (`databricks/pipelines/reporting.sql`) builds `investigations_flat`, `investigations_daily`, `canary_trend` and `audit_chain_links` from the Delta tables. No dashboard shows them yet.
- `aml-canary` job (Mon 06:00 UTC) runs the 7-account health check in `evaluation/`, stores rows in `eval_runs`, and fails if a tier or typology is wrong or fewer than 90% of draft sentences are supported.

## Quick answers for the whiteboard question

| Question | Answer |
|---|---|
| Where is the RAG? | Step 3b: ChromaDB over `typology_docs/`, queries built from the evidence, a model judges, code validates. |
| Where are the agents? | Four stages, each a small LangGraph graph: evidence, typology, narrative, faithfulness check. They are pipeline stages that use a model, not autonomous programs. |
| Where is the ML? | There is none for scoring: the risk score is rules. Models are used only to summarize, judge, write and fact-check. |
| What stops the model from making things up? | Code-side checks after every model call (steps 3b to 3d), then a human review. |
| What happens if a database is down? | The run is `incomplete`, never "all clear". |
| What is stored? | The draft, checks and hash; never the raw rows. The audit trail is append-only and hash-chained. |
| What did a run take? | About 150 s per account with Gemini and about 30 s with Claude in the 21-account test, plus several minutes of job start-up. Under load: not measured. |
