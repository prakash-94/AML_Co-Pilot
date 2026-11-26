# FLOW.md - how the system works, start to finish

How to read this file
- Every file and function name below was checked against the code on 2026-11-19, and the call order was re-checked line by line on 2026-11-23 (see "Corrections made on 2026-11-23" at the end). "D-0xx" points to DECISIONS.md (the choice and why), "C-0xx" to its challenge log.
- UNKNOWN means the repository does not say. Nothing is guessed.
- Plain-English words used: a "job" is one run of a program on Databricks; a "model" is the AI that reads and writes text; "RAG" means look up relevant written passages first and let the model use them; a "hash" is a fingerprint of some data that changes if the data changes.

## 1. Purpose (one sentence)
Turn one flagged bank account number into a fact-checked draft of a Suspicious Activity Report (SAR) narrative, for a person to approve, reject or escalate, without ever filing anything itself.

## 2. Whiteboard diagram (boxes and arrows, text only)

```
[Reviewer types an account id]
            |
            v
[Web page (Databricks App)] --Run now--> [Databricks job: aml-investigate]
            |  (the notebook or terminal tool are other ways to review)
            v
   0  CHECK AND CLAIM: valid id? under the daily limit (30)? already open? claim it once; audit log starts
            |
            v     (the four steps run ONE AFTER THE OTHER; any error skips straight to step 5)
   1 EVIDENCE ---> 2 TYPOLOGY (RAG) ---> 3 NARRATIVE ---> 4 FACT CHECK
            |
   1 EVIDENCE: 3 data sources (Databricks, Salesforce, Supabase, queried at the same time),
               follow linked accounts, rule score 0-100 (no ML)
   2 TYPOLOGY: search typology documents in ChromaDB; model judges; code drops matches
               citing evidence it never saw
   3 NARRATIVE: model drafts the SAR text; code drops sentences with figures not in the
               evidence (skipped if the risk is low and nothing matched)
   4 FACT CHECK: a second model call must quote proof for every sentence; code verifies
               the quote really exists (only if a draft was written)
            |
            v
   5  CONCLUDE: status = draft_ready | no_action | incomplete | failed
            |
            v
[Delta tables: result (no raw rows, with a hash) + hash-chained audit log]
            |
            v
   6  HUMAN REVIEW: approve / reject / escalate (only a supervisor can approve; nothing is ever filed)
            |
   7  REPORTING and the weekly CANARY (run separately, not part of one investigation)
```

## 3. Step by step

### Step 0 - Start, check and claim the run
- What happens: A reviewer enters an account id and picks a model family (Gemini or Claude) on the web page. The page asks Databricks to run the investigate job. The job loads its settings and secrets, builds the document index, checks the request, and claims the run so it can only run once.
- Files / functions:
  - `dbx/static/app.js` -> `startInvestigation` -> `api` (adds the header `X-Requested-With: aml-copilot`) -> `POST /api/investigations`. Choosing "Both" sends two requests (Gemini, then Claude); the job runs them one after the other. Then `watch` asks `GET /api/runs/{run_id}` every 5 seconds until the run ends, finds the new investigation in `GET /api/investigations` (same account, same model family, created after the click), and opens it (or compares the two).
  - `dbx/webapp.py` -> `create_app` -> route `POST /api/investigations` -> `start` (checks sign-in via `current_person`, the header via `require_same_origin`, and the account id and `provider` via `StartBody`), then `run_status` for `GET /api/runs/{run_id}`.
  - `dbx/jobs_api.py` -> `JobsApiLauncher.start` calls the Databricks Jobs API `run-now` with `account_id`, `requested_by`, `provider`.
  - `databricks/jobs/investigate.py` -> `dbx/jobs.py` -> `investigate_main`.
  - Inside `investigate_main`, in this order: `provider_settings` (`dbx/providers.py`; adds the Claude settings for a Claude run); `_prepare` -> `apply_config`, `load_secrets` (`dbx/runtime.py`), `Settings` (`config.py`), `configure_logging`; the DDL from `create_tables_sql` (`service/delta.py`); `spark_services` (`dbx/runtime.py`); the requester (the `--requested-by` value, or the job's own `current_user()` if blank); `build_index` (`retrieval/store.py`); then `make_run_fn` and `make_provenance` (`service/pipeline.py`) are created and handed to `investigate` (`dbx/investigate.py`).
  - `investigate` -> `validate_account_id` (`ids.py`) -> `new_job` and `JobStore.submit` (`service/jobs.py`) -> `AuditLog.append("JOB_SUBMITTED")` (`service/audit.py`) -> `process_job` (`service/worker.py`) -> `JobStore.claim` -> `AuditLog.append("RUN_STARTED")` -> the `run` function made by `make_run_fn`, which calls `run_investigation` (`agents/investigation.py`).
- Called when: A person presses Investigate (or "Run now" on the job).
- Why it exists: The page never runs the expensive pipeline itself (D-018); the rules stop double runs and runaway spending (D-014); the one-run-at-a-time job setting protects the Delta stores (D-019); the job signs in with a service principal (D-020); Supabase is reached by its IPv4 address (D-024).
- Other options: Running the pipeline inside the web server - UNKNOWN whether considered (D-018).
- If it fails: An invalid id is refused before any job record or audit event is written (the page answers 422; the job raises `InvalidAccountIdError`). Note the job has already created its tables if missing and built the document index by then, because `investigate` validates the id after those set-up steps. A duplicate open run for the same account returns the open job (outcome "duplicate", counted as fine). Past 30 runs in a UTC day the job fails visibly ("quota_exceeded", exit code 1). A request that is not from our page gets 403; no sign-in gets 401; an unlisted person gets 403; a Databricks Jobs API error becomes a 502. (See C-010 for the job settings that once broke here.)
- Hands to next step: A claimed job record (status RUNNING) and the account id.

### Step 1 - Evidence gathering
- What happens: Pulls transactions (Databricks), the customer profile (Salesforce) and earlier cases (Supabase) for the account at the same time. Merges and cleans them, confirms every record belongs to this account, follows wire-transfer links to other accounts (max 4 hops, 30 accounts, 500 rows), runs the two rule detectors, and asks a model for a factual, cited summary.
- Files / functions:
  - `agents/investigation.py` -> `run_investigation` -> `_evidence_node` -> `agents/evidence.py` -> `investigate` -> `build_evidence_graph` (nodes `ingest`, `score`, `summarize`).
  - `ingest`: `ingestion/pipeline.py` -> `ingest` -> `run_ingestion`: first `validate_account_id` again (a second check of the same id); then a `ThreadPoolExecutor` runs `_safe_fetch` on `DatabricksSource.fetch`, `SalesforceSource.fetch`, `SupabaseSource.fetch` (`ingestion/sources.py`, each wrapped by `call_with_retry` in `ingestion/retry.py`); then `merge_sources`; `normalize_transactions`, `normalize_profile`, `normalize_cases`, `resolve_entity` (`ingestion/normalize.py`); then `expand_network` (`ingestion/expansion.py`) using `DatabricksSource.fetch_around`.
  - `score`: `detection/risk_score.py` -> `run_detection` -> `detect_structuring` (`detection/structuring.py`), `detect_layering` (`detection/layering.py`), `score_account`.
  - `summarize`: `build_evidence_prompt`, then the model (client from `make_chat_model` in `agents/llm.py`; the function is still named `make_anthropic_summarizer`) with `invoke_structured`, then `with_derived_citations`. An account with no data skips the model (`_is_empty`, `_empty_summary`).
- Called when: First, after the claim in Step 0.
- Why it exists: The model may only see evidence the system gathered itself, so every claim can be traced to a source. Rules produce the score (D-001), the answer column is never read (D-005), linked accounts reveal layering (D-004, D-003), an outage is recorded and never read as all-clear (D-006), and temporary errors are retried (C-017).
- Other options: Query only the one account - rejected, layering chains are invisible to a one-account query (D-004). A model-based score - see D-001 (ML rejected for now). Other options for sources: UNKNOWN.
- If it fails: A source that cannot be queried becomes an entry in `missing_sources` (not an error). A record that belongs to another account, or two Salesforce profiles for one account, raises `EntityResolutionError`; a missing data-source setting raises `ConfigError`; a model answer that does not fit its shape (after one retry) raises `ModelOutputError`. These end the run as `failed` at the evidence step. A missing model setting (for example a missing API key) raises `ConfigError` before the graph starts, so `process_job` records the whole job as FAILED instead. When network expansion hits a limit it only adds a warning to the evidence; the code sets an internal `complete = False` flag (`Expansion.complete` in `ingestion/expansion.py`) but nothing reads that flag, so the evidence is not marked incomplete and the run status does not change.
- Hands on: `bundle` (raw evidence, kept in memory only), `summary` (`EvidenceSummary` with citations) and `risk_score`.

### Step 2 - Typology match (RAG)
- What happens: Builds up to three search queries from the summary, finds the closest passages in the typology documents, and asks a model which documented patterns the evidence resembles. Code then checks the answer.
- Files / functions: `agents/investigation.py` -> `_typology_node` -> `agents/typology.py` -> `match_typology` -> `build_typology_graph` (nodes `retrieve`, `match`). `retrieve`: `build_queries` (detection, network and transaction summaries), `retrieval/store.py` -> `search` (`doc_type="typology"`, 5 per query), `merge_passages` (distance at most 0.75, best 8). `match`: `build_typology_prompt` -> the judge from `make_anthropic_judge` -> `finalize_assessment` (uses `clean_ids`, `rule_engine_ids`, `verdict_words` in `agents/checks.py`). The index itself was built in Step 0 by `build_index` from `typology_docs/` (`retrieval/chunking.py`).
- Called when: After Step 1 for every account unless Step 1 failed. (Correction to the template: it does not wait for a "needs more than no action" test. That test is in Step 3.) It skips the model when the summary has no citations ("No evidence available to match.") or no passage is close enough ("No typology passage was similar enough to the evidence.").
- Why it exists: Ground the judgement in real guidance instead of the model's memory (D-007). Only knowledge passages are searched (D-008). Distance cannot decide a match, so it is a noise filter and the model decides (D-009). A stronger model judges (D-010). Code removes matches that cite a document that was not retrieved, evidence ids that do not exist, or no supported red flag (D-002, C-006), and sets `detector_corroborated` itself.
- Other options: UNKNOWN - ask the owner (for example all documents in the prompt, or a hosted vector database; D-007).
- If it fails: A search or model error ends the run as `failed` at the typology step. Dropped matches are listed in the assessment's `warnings`, never silently.
- Hands on: `assessment` (`TypologyAssessment`: matches, passages shown, warnings).

### Step 3 - Narrative draft
- What happens: Decides in plain code whether a draft is worth writing; if so, loads the investigator questions from the matched typology files and asks a model for the three parts FinCEN recommends (introduction, facts, conclusion) as cited sentences. Code then checks every sentence.
- Files / functions: `agents/investigation.py` -> `_narrative_node` -> `agents/narrative.py` -> `write_narrative` -> `build_narrative_graph` (nodes `prepare`, `write`). `prepare`: `should_draft`, then `retrieval/questions.py` -> `questions_for_sources`. `write`: `build_narrative_prompt`, the writer from `make_anthropic_writer`, `finalize_narrative` (`_unsupported_details`, `clean_ids`, `verdict_words`). Writing rules mirror `typology_docs/sar_narrative_standards.md`.
- Called when: After Step 2. A draft is written only if a typology matched or the risk tier is medium or high. Otherwise the narrative has status `not_drafted` and the model is not called.
- Why it exists: To save an investigator the slow writing. Sentences with a dollar amount, date or id that appears nowhere in the evidence are dropped (D-002); an amount with no id, verdict-like wording ("suspicious", "should file", and so on) and rule-engine transactions no sentence cites raise warnings. Investigator questions are copied word for word from the documents, never written by the model.
- Other options: UNKNOWN.
- If it fails: A model or schema error ends the run as `failed` at the narrative step. If every sentence of a section fails validation, a warning says so.
- Hands on: `narrative` (`SarNarrative`: status, sentences with ids, questions, warnings).

### Step 4 - Fact check
- What happens: A second model call rules on every sentence (supported, unsupported or contradicted) and must quote the evidence. Plain code verifies that each quote appears in the evidence; a "supported" verdict without a real quote becomes "unverified". Free exact checks also compare the score, tier, prior-case count and typology names with the facts.
- Files / functions: `agents/investigation.py` (`_faithfulness_node`, chains the step) -> `guardrails/faithfulness.py` -> `run_faithfulness` -> `check_faithfulness` -> `build_faithfulness_prompt`, the judge, `finalize_faithfulness` (`quote_in_evidence`) and `guardrails/consistency.py` -> `check_consistency`.
- Called when: After Step 3, only if a draft was written. It is on by default (`check_faithfulness=True`; the command line has `--no-faithfulness`).
- Why it exists: A model must not be the only judge of its own output (D-011). Flagged sentences go to a human and are never edited automatically (D-012).
- Other options: Plain code checks only (cannot judge meaning); no check at all. A third-party evaluation library: UNKNOWN (D-011).
- If it fails: An error raised while checking (for example a model answer that does not fit its shape, `ModelOutputError`) is recorded as `faithfulness_error`, the draft is still delivered, and the status text says the draft "has not been fact-checked". A missing model setting is different: `run_faithfulness` builds the judge before its `try` block, so that `ConfigError` is not recorded as `faithfulness_error` and would crash the job (FAILED). In practice an earlier step would already have failed for the same missing setting.
- Hands on: `faithfulness` (`FaithfulnessReport`: counts, flagged sentences, consistency issues, pass or fail).

### Step 5 - Conclude and store
- What happens: Works out what the run means, turns the result into the stored form (without raw rows), fingerprints it, saves it and writes the audit event.
- Files / functions: `agents/investigation.py` -> `_conclude_node` -> `decide_outcome` and `collect_warnings`. Back in `service/worker.py` -> `process_job`: `service/models.py` -> `to_api_result` (reads only named keys, never `bundle`; it also records which models were used, from `make_provenance`, and each step's seconds, which `run_investigation` measures through its `observer`) and `compute_result_hash` (SHA-256); `JobStore.complete` (`service/jobs.py`) -> `DeltaDocuments.transact` (`service/delta.py`, one versioned `MERGE`); `AuditLog.append("RUN_FINISHED")` (`service/audit.py`, `DeltaObjects.put_if_absent`). Then `dbx/investigate.py` returns an `InvestigationReport` and `dbx/jobs.py` -> `investigate_main` prints `RESULT ...` and returns 0 (ran or duplicate) or 1.
- Called when: After Step 4 (or straight after the first failure).
- Why it exists: The status rules (D-006): `failed` if a step errored; `draft_ready` if a narrative was drafted (noting any missing source and any fact-check flags); `incomplete` if nothing was drafted but a source was down; otherwise `no_action`. The result has no field for raw rows (hard rule). The audit trail is hash-chained and append-only (D-013). A draft ready for review gets review status PENDING_REVIEW.
- Other options: UNKNOWN.
- If it fails: A crash inside the pipeline is caught by `process_job`: the job is marked FAILED with a short error (at most 500 characters), `RUN_FAILED` is audited, the account's open marker is released, and it is never retried because a retry would be a second paid run. If two writers collide, `DeltaDocuments.transact` retries up to 3 times; a write that lands only partly raises `PartialWriteError`, which needs a human look.
- Hands on: A stored job (`documents` table) and audit events (`audit_objects` table).

### Step 6 - Human review
- What happens: A reviewer lists investigations, opens one (draft, fact check, warnings, audit trail, check that the audit chain is intact), and approves, rejects or escalates with a comment. Two drafts can be opened side by side.
- Files / functions: `dbx/webapp.py` routes `GET /api/me`, `GET /api/investigations`, `GET /api/investigations/{job_id}`, `GET .../audit` (`AuditLog.verify_chain`), `POST .../decision` -> `dbx/review.py` -> `decide` -> `JobStore.decide` -> `AuditLog.append("DECISION")`. The same logic serves `databricks/notebooks/review.py` and `scripts/dbx_review.py` through `run_action`. Roles come from `reviewer_roles` (`dbx/runtime.py`; the page caches them for 30 seconds in `dbx/appmain.py`). The page itself: `dbx/static/index.html` and `app.js`. The app starts from `databricks/app/main.py` -> `build_from_environment`.
- Called when: After a run ends as `draft_ready`.
- Why it exists: A person must decide, never the system (D-015). The reviewer is the real signed-in Databricks login. A decision is final and bound to the draft's hash. Three ways to review exist because the Databricks web login was unreliable (C-015, D-025).
- Other options: UNKNOWN (D-015).
- If it fails: Not listed in `reviewers` gives 403; an investigator trying to approve gives 403; a second decision, or a job that is not decidable, gives 409; a draft whose hash does not match (web page only) gives 409; an empty comment gives 422; an unknown job gives 404.
- Hands to next step: The decision record and the `DECISION` audit event. Nothing is filed with any regulator.

### Step 7 - Reporting and the weekly canary (outside one investigation)
- What happens: The `aml-canary` job (Mondays 06:00 UTC) runs the pipeline on a small set of accounts with known right answers, writes one row per account to the table `eval_runs`, refreshes the reports, and fails if quality dropped. The canary calls `run_investigation` directly: it does not go through Steps 0 and 5, so it creates no job record, no daily-limit count and no audit events. The `aml-refresh-reports` job (daily 07:00 UTC) refreshes the `aml-reporting` pipeline.
- Files / functions: `databricks/jobs/canary.py` -> `dbx/jobs.py` -> `canary_main` (loads settings and secrets like Step 0, builds the index, then calls `evaluation_main(["canary", ...])`) -> `evaluation/__main__.py` -> `cmd_canary` -> `evaluation/dataset.py` `sample_cases` (a fixed, seeded choice of accounts) -> `evaluation/runner.py` `evaluate` -> `live_run_fn` -> `run_investigation` -> `evaluation/metrics.py` `score_case`; then `make_sink("spark", ...)` -> `evaluation/sinks.py` `DeltaSink` writes the rows (`result_row`); finally `regressions` (`MIN_SUPPORTED_RATE = 0.90`) decides the exit code. A spending cap (`BudgetMeter`) stops the run early if exceeded. Reports: `databricks/pipelines/reporting.sql` (`investigations_flat`, `investigations_daily`, `canary_trend`, `audit_chain_links`).
- Called when: On the schedules in `databricks.yml`.
- Why it exists: See D-027 and D-028.
- Other options: UNKNOWN.
- If it fails: The canary job fails (exit code 1) if any account's run failed, ended `incomplete`, got the wrong risk tier or typologies, or was drafted when it should not have been (or the reverse); if fewer than 90% of all checked draft sentences are supported; or if fewer accounts were evaluated than planned (for example the spending cap stopped it). A flagged draft on its own does not fail it. A problem storing the rows only prints a warning. The reports are refreshed anyway (`run_if: ALL_DONE`).
- Hands to next step: Rows in `eval_runs` and the four report views. No dashboard displays them yet.

## 4. How the files connect (who calls whom)

```
databricks/jobs/investigate.py
 └─ dbx/jobs.py: investigate_main
     ├─ dbx/providers.py: provider_settings          (Claude overrides the model settings)
     ├─ dbx/jobs.py: _prepare -> dbx/runtime.py: apply_config, load_secrets; config.py: Settings
     ├─ service/delta.py: create_tables_sql          (documents, audit_objects, reviewers)
     ├─ dbx/runtime.py: spark_services               (JobStore, AuditLog, role lookup on Delta)
     ├─ retrieval/store.py: build_index              (ChromaDB from typology_docs/)
     ├─ service/pipeline.py: make_run_fn, make_provenance   (creates the function that will run the pipeline)
     └─ dbx/investigate.py: investigate
         ├─ ids.py: validate_account_id
         ├─ service/jobs.py: JobStore.submit
         ├─ service/audit.py: AuditLog.append        (JOB_SUBMITTED)
         └─ service/worker.py: process_job
             ├─ service/jobs.py: JobStore.claim / complete / fail
             ├─ service/audit.py: RUN_STARTED, RUN_FINISHED or RUN_FAILED
             ├─ service/models.py: to_api_result, compute_result_hash
             └─ the function from make_run_fn -> agents/investigation.py: run_investigation
                 ├─ evidence.py: investigate
                 │    ├─ ingestion/pipeline.py: ingest -> run_ingestion
                 │    │    ├─ ingestion/sources.py (3 sources) + ingestion/retry.py
                 │    │    ├─ ingestion/normalize.py
                 │    │    └─ ingestion/expansion.py: expand_network
                 │    ├─ detection/risk_score.py: run_detection (structuring.py, layering.py)
                 │    └─ agents/llm.py: make_chat_model, invoke_structured
                 ├─ typology.py: match_typology -> retrieval/store.py: search
                 ├─ narrative.py: write_narrative -> retrieval/questions.py
                 ├─ guardrails/faithfulness.py: run_faithfulness (+ consistency.py)
                 └─ decide_outcome (inside the conclude step)

agents/llm.py -> agents/gcp_auth.py (keyless Google login) -> ingestion/databricks_auth.py

Web page: dbx/static/app.js -> dbx/webapp.py -> dbx/jobs_api.py (start the job) and dbx/review.py (decisions)
          built by dbx/appmain.py; launched by databricks/app/main.py
Other review tools: databricks/notebooks/review.py, scripts/dbx_review.py -> dbx/review.py
Weekly check: databricks/jobs/canary.py -> dbx/jobs.py: canary_main -> evaluation/__main__.py: cmd_canary
              -> runner.evaluate -> live_run_fn -> agents/investigation.py: run_investigation (no job record, no audit)
              -> metrics.score_case, sinks.DeltaSink, metrics.regressions
```

## 5. What can go wrong -> what the system does

| What goes wrong | What the system does |
|---|---|
| Bad or hostile account id | Refused by `validate_account_id` before any query or write |
| Same account asked for twice | Returns the already-open run (outcome "duplicate") |
| More than 30 runs in a UTC day | Run refused and the job fails visibly |
| A data source is down | Recorded in `missing_sources`; draft says evidence is incomplete, or the run ends `incomplete` (never all-clear) |
| Warehouse waking, or a brief network error | Retried with growing waits (Databricks 4 tries, Salesforce 3, Supabase 3); Salesforce login failures are never retried |
| Records of another account in the results | `EntityResolutionError`; run ends `failed` at evidence |
| Network expansion hits a limit | A warning is added to the evidence. Nothing marks the evidence or the run incomplete (the internal `complete` flag is never read) |
| Model answer has the wrong shape | One retry, then `ModelOutputError`; run ends `failed` at that step |
| A model call hangs | Gemini calls stop after 300 seconds and retry up to 2 times (C-014). Claude calls have no deadline set in the code |
| Missing setting or key (for example `ANTHROPIC_API_KEY` for a Claude run) | `ConfigError` "Missing required settings"; the job is marked FAILED; Gemini runs are unaffected |
| Model invents a figure, date or id in the draft | The sentence is dropped and a warning explains |
| Model cites evidence that was never retrieved | The match is dropped and a warning explains |
| Fact check cannot run | Draft still delivered, flagged as not fact-checked |
| Fact-check quote is not in the evidence | The verdict becomes "unverified" and goes to a human |
| Crash in the pipeline | Job marked FAILED, `RUN_FAILED` audited, no retry |
| Two writers change the same record | Versioned write refuses; up to 3 attempts; a partly written change raises `PartialWriteError` |
| Someone edits the audit log | `verify_chain` reports the first broken event; Delta refuses UPDATE and DELETE on the table |
| Approving a changed draft (web page) | 409; nothing changes |
| Unlisted person, or an investigator approving | 403 |
| App stopped by Free Edition (about 24 hours) | Page unavailable; restart with `python scripts/dbx_deploy.py run copilot` (docs) |

## 6. Numbers to remember

| Item | Value | Where it is set |
|---|---|---|
| Data (synthetic) | about 100K transactions, 2,100 customer profiles, 47K case records (the README states these; exact live counts are not recorded in the repository) | `README.md` |
| Structuring rule | at least 3 cash deposits from $7,000 up to (not including) $10,000 within 90 days | `detection/structuring.py` |
| Layering rule | a chain of at least 2 transfers completed within 72 hours; shrinking amounts and crossing borders are reported, not required | `detection/layering.py` |
| Risk score | 50 points per signal; high from 70, medium from 30 | `detection/risk_score.py` |
| Network expansion | 4 hops, 30 accounts, 500 rows, wire transfers only | `ingestion/expansion.py` |
| Retrieval | 5 passages per query, at most 8 shown, distance at most 0.75; chunks of 220 words with 40 overlap | `agents/typology.py`, `retrieval/chunking.py` |
| Daily limit; duplicate lock | 30 runs per UTC day; 10 minutes | `service/jobs.py`, `databricks.yml` |
| Model calls | Gemini 300 s deadline, 2 retries; answer budget 4,096 tokens plus 8,192 for hidden thinking | `agents/llm.py` |
| Tests | 577 passing (2026-11-19) | `pytest -q` |
| Evaluation (21 synthetic accounts, same sample, README and reports) | tier 100% correct; typology precision and recall 100%; 0% false alarms on normal accounts; sentences supported 99% of 236 (Claude) and 97% of 203 (Gemini); drafts passing the fact check outright 80% (Claude) and 67% (Gemini) | `evaluation_results/*.md`, `README.md` |
| Pipeline cost on the 21 accounts | Claude $0.88; Gemini $3.36 (the README's Claude figure of $2.63 also includes judge testing) | `evaluation_results/*.md` |
| Time per account | Gemini about 153 s (report); Claude about 30 s (README only) | `evaluation_results/gemini_report.md`, `README.md` |
| Weekly canary | Mondays 06:00 UTC; default 7 accounts (N 2, S 2, L 2, H 1); fails below 90% sentences supported; cost cap 1.6 dollars in the bundle | `databricks.yml`, `dbx/jobs.py`, `evaluation/metrics.py` |
| Secret lifetime | the service principal's secret expires after 90 days | `docs/databricks.md` |

## 7. Known limits (say these before the interviewer finds them)
- Only two typologies (structuring, layering) have detectors. The other five are retrieved by similarity only and marked "no detector yet"; they need data the project does not collect (`typology_docs/README.md`).
- All data is synthetic. How the transactions and case records were generated is UNKNOWN (only the Salesforce profile generator and the high-risk seed script are in the repository). Right answers for testing come from the account id prefix, so a detector "miss" may reflect how an account was generated.
- The evaluation is 21 accounts and one seed: enough to find problems, not for confidence figures. Not measured: behaviour on real bank data, agreement with human investigators, load, how long 5,000 customers would take, and time saved against a manual investigation.
- The fact check judges a draft against the evidence summary, not the raw rows. If the summary is wrong, a faithful draft inherits the error. The judge can contradict its own reasoning, which is why flagged sentences go to a human (D-012).
- A layering flag needs only a chain of 2 or more transfers within 72 hours. Jurisdiction risk is described in the evidence but not scored.
- When network expansion hits a limit (hops, accounts or rows) it only adds a warning. The run is not marked incomplete, so a draft can rest on a cut-off chain with nothing but that warning to say so.
- In the notebook and terminal tool, the "decision is tied to the draft hash" check cannot catch a draft that changed after it was displayed; only the web page sends and compares the hash it showed (D-015).
- The duplicate lock lasts 10 minutes; one run at a time (D-019) is the second guard. Throughput is not measured.
- Each job installs its libraries first, so a run takes minutes before the first query. Databricks Free Edition: serverless only, restricted outbound internet, at most 3 apps, an app stops after about 24 hours, an inactive workspace may be deleted.
- Gemini 3.1 Pro is a preview model. The service principal's secret needs rotating every 90 days.
- Claude runs need `ANTHROPIC_API_KEY` in the Databricks secret scope. Whether it is there now: UNKNOWN from the repository (the owner can check by starting a Claude run).
- The reporting tables exist but no dashboard displays them. The audit trail is tamper-evident, not tamper-proof (D-013).
- The 577 tests use fakes for every outside system, so the real systems are checked only by manual runs (`sample_runs/`) and the weekly canary.
- Documentation that no longer matches the code (C-020):
  - `README.md` says 550 tests (actual 577); its results table compares unlike costs (see DECISIONS.md question 15).
  - `docs/phase0-checklist.md` is the Google Cloud setup checklist, kept in the repository after that runtime was removed.
  - Docstrings still mention Cloud Run, Cloud Tasks, Firestore or Cloud Storage: `service/jobs.py`, `service/audit.py`, `dbx/investigate.py`. Several agent docstrings say "Claude" although Gemini is the default.
  - `retrieval/store.py` says the documents live in a Unity Catalog Volume, but `databricks.yml` points the job at the workspace files folder.
  - `agents/gcp_auth.py` says "Two modes" and lists four; `errors.py` still says the id format is `[S|L|N]` (the code also accepts `H`).
  - The CI workflow lints `aml_copilot`, `tests` and one script, while the deploy workflow lints the whole repository (`ruff check .`).

## 8. Target: what "done" means (PLANNED, not built yet)
The work toward production-level practice (D-030) on the branch `production-hardening` (D-035) is done when all of these are true. Nothing in this section describes how the system works today.
- [ ] A cut-off money chain is visible on the draft and the report (whether it also changes the run status is not decided yet).
- [ ] Claude calls have a deadline.
- [ ] Every way of approving refuses a draft that changed after it was shown.
- [ ] Every run records the code version, token use and an estimated cost.
- [ ] A PDF can be downloaded for each case, marked as a draft and not a regulatory filing, with no raw transaction rows (D-031, D-034).
- [ ] A manager summary PDF exists, showing time saved from a timed trial or "Not measured yet" (D-033).
- [ ] A quality check with more accounts, including bad data, runs before release.
- [ ] Customer names are protected before model calls, hostile text in data fields is tested, and a threat list is written.
- [ ] Job failures raise alerts, there is a separate test target if Free Edition allows it, library versions are fixed, and a runbook exists.
- [ ] A small batch has been timed and the one-run-at-a-time rule (D-019) has been reviewed.
- [ ] The outdated text listed in section 7 is fixed.
- [ ] The full test suite and lint pass, with no fewer than 577 tests.

## Corrections made on 2026-11-23 (FLOW.md checked against the code)
Every backticked file and function name was checked to exist, and each step's call order was read in the code. All names exist. These statements were wrong or incomplete and are now fixed:
1. Section 5 (and Step 1) said a network-expansion limit "marks the evidence incomplete". It does not: only a warning is added; the `complete` flag inside `Expansion` is never read (`ingestion/pipeline.py` uses only `net.warnings`). Also added to Section 7. DECISIONS.md D-004 had the same wrong wording and was corrected on 2026-11-23.
2. The Section 2 diagram drew Steps 1 to 4 side by side as if parallel. They run one after the other.
3. Step 7 (canary) listed only some failure causes and implied it follows the normal job path. It calls `run_investigation` directly (no job record, no audit) and fails on any failed or incomplete run, wrong tier or typology, wrong draft decision, fewer than 90% sentences supported, or fewer accounts evaluated than planned. The full function chain is now listed.
4. Section 4 showed `make_run_fn` being called by `process_job`. It is created in `investigate_main` and handed to `investigate`; `process_job` only calls the function it returns. `_prepare` and `make_provenance` were missing and are added.
5. Step 0 said an invalid id is refused "before anything is written". The tables are created and the index is built first; only job records and audit events are not written.
6. Step 0 cited C-014 (a model-call hang) as a start-up failure. Removed; it is now cited in Section 5 where model calls are discussed.
7. Step 0 now includes how the page follows a run (`api`, `watch`, polling `GET /api/runs/{run_id}` every 5 seconds, and the "Both" option that sends two requests).
8. Step 1 now notes that `run_ingestion` validates the account id a second time.
9. Step 4's failure text said any failure is recorded as `faithfulness_error`. A missing model setting is raised before the `try` block and would crash the job instead.
10. Step 5 now includes provenance (models used) and step timings. Section 5 notes that Claude calls have no deadline set.
