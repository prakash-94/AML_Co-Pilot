# DECISIONS.md - what we chose, when, and why

How to read this file
- Every fact here was checked against the code, tests, docs, README or git history on 2026-11-19. Each entry has an "Evidence" line saying where.
- Dates come from git. The repository's history starts on 2026-10-11, and its first real commit (`bbdd4c5`) holds everything built before that day. Work from before then cannot be dated, so it says "by 2026-10-11 (exact date UNKNOWN)".
- UNKNOWN means the repository does not say. The owner must answer; nothing here is a guess. All UNKNOWN items are collected at the end.
- "Who decided" is UNKNOWN unless the owner stated the choice (D-001 and D-002 reasons were stated by the owner).
- Test count: 577 passing on 2026-11-19. No test has been removed in the recorded history.

## D-001 - Score risk with plain rules, not a trained ML model
- Date: by 2026-10-11 (exact date UNKNOWN; the 2026-09-28 date in the owner's template cannot be confirmed from git)
- Status: Accepted
- The problem: Give each flagged account a 0-100 risk score.
- Options considered:
1. Plain rules (two detectors, up to 50 points each) - good: explainable to an investigator, needs no training data / bad: fixed thresholds, a new pattern needs a new rule
2. Trained ML model - good: can learn patterns nobody wrote down / bad: needs training data and is harder to explain
- Decision: Plain rules. ML stays a possible later addition.
- Why: The score must be explainable, and no training data exists (both reasons stated by the owner).
- Trade-off we accepted: Only two typologies have detectors. Jurisdiction risk is described in the evidence but not scored.
- Who decided: owner (stated in the project notes)
- Revisit when: real labelled data exists to judge an ML detector fairly.
- Evidence: `detection/risk_score.py` ("Purely mechanical - no LLM here", weights 50 + 50, high from 70, medium from 30); `README.md`; `typology_docs/README.md` (five typologies have no detector).

## D-002 - The model writes, plain code checks
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: Models vary from run to run, even at temperature 0, and can invent facts. A bank draft cannot contain invented amounts or dates.
- Options considered:
1. Trust the model's own self-check - bad: a model policing itself
2. A second model call that must QUOTE proof, then code verifies the quote exists - good: the quote is checkable
3. UNKNOWN - ask the owner whether others were considered
- Decision: Option 2, plus code-side rules after every model call: a typology match citing evidence that was never retrieved is dropped; a narrative sentence with an amount, date or id found nowhere in the evidence is dropped; `detector_corroborated` is set by code and never shown to the model.
- Why: The owner's reason is above. In the code, `finalize_assessment`, `finalize_narrative` and `finalize_faithfulness` all say the model "is allowed to be wrong; this is where that stops mattering".
- Trade-off we accepted: Extra model calls cost extra money and time. The fact check passed 80% of Claude drafts and 67% of Gemini drafts outright, so about 20% (Claude) to 33% (Gemini) go to a human flagged.
- Who decided: owner (stated in the project notes)
- Revisit when: UNKNOWN - ask the owner.
- Evidence: `agents/typology.py`, `agents/narrative.py`, `guardrails/faithfulness.py`; `README.md` results table.

## D-003 - Detect structuring over a 90-day window, not 7 days
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: Structuring means splitting cash deposits to stay under the $10,000 reporting limit. A short detection window missed a real structuring account in the data.
- Options considered:
1. 7-day window - good: UNKNOWN / bad: "missed it entirely" on live data (the account spread its deposits about 52 days apart across three branches)
2. 90-day window (one quarter) - good: catches it, and 90 days is described in the code as a common monitoring lookback / bad: UNKNOWN
- Decision: 90 days. A flag needs at least 3 cash deposits of $7,000 up to (not including) $10,000 inside the window.
- Why: The code comment above. Why the floor is 70% of the limit and the count is 3: UNKNOWN.
- Trade-off we accepted: UNKNOWN - ask the owner.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `detection/structuring.py` (`StructuringConfig`, comment on `window_days`); `typology_docs/structuring.md` (repeats the 52-day story). See C-001.

## D-004 - Follow the money to linked accounts, within limits
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: Layering hides money in a chain of accounts, so one account's own rows show only the first step.
- Options considered:
1. Query only the flagged account - bad: layering chains are invisible (stated in `ingestion/expansion.py`)
2. Walk outward breadth-first over counterparty links in both directions, with limits - good: finds chains; limits keep it bounded
3. UNKNOWN - ask the owner whether an unlimited walk was considered
- Decision: Option 2: up to 4 hops, 30 related accounts and 500 rows, following wire transfers only. Hitting a limit adds a warning to the evidence, so it is never silent. (The code also sets an internal `complete = False` flag, but nothing reads it: the evidence and the run status are not marked incomplete. Corrected 2026-11-23 after a code check.)
- Why: The chain reason above. Why these exact numbers: UNKNOWN.
- Trade-off we accepted: A long chain or a busy account can be cut off, and the only sign is a warning; the run is not marked incomplete.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `ingestion/expansion.py` (`ExpansionConfig`, `expand_network`), `ingestion/pipeline.py`; `tests/test_expansion.py`.

## D-005 - Agents must never see the answer column
- Date: by 2026-10-11 (exact date UNKNOWN); the label-free view was in the first commit's description
- Status: Accepted
- The problem: The transactions table has a hidden `ground_truth_label` (what pattern each synthetic account was built with). If an agent saw it, every measurement would be meaningless.
- Options considered:
1. Rely on code never selecting the column - bad: one slip leaks it
2. Several layers: the code lists only safe columns and strips the label again after reading; jobs read a Unity Catalog view (`transactions_safe`) that does not contain it; the job's login can read only that view
3. UNKNOWN - ask the owner
- Decision: Option 2. Expected answers for testing come from the account id prefix instead (`evaluation/dataset.py`).
- Why: The label is the answer key. The README says the label and the base table were refused to the job's login "tested live".
- Trade-off we accepted: UNKNOWN
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `ingestion/sources.py` (`select_safe_columns`), `ingestion/normalize.py` ("defence in depth"), `databricks.yml` (default table is `transactions_safe`), `docs/databricks.md`, `README.md`. Note: the code probes column names with `SELECT * ... LIMIT 0`, which returns no rows.

## D-006 - A source outage means "incomplete", never "all clear"
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: If a data source is down, the evidence is thin, nothing matches, and the result would look exactly like a clean account.
- Options considered:
1. Treat missing data as "no action" - bad: an outage would hide a real case
2. Record the missing source and end as `incomplete` unless the evidence was complete
3. UNKNOWN
- Decision: Option 2 (`decide_outcome`). A draft made from partial evidence is still `draft_ready` but says which source was missing.
- Why: Stated in the module docstring of `agents/investigation.py`.
- Trade-off we accepted: Runs with a source down need a re-run.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `agents/investigation.py`; `ingestion/pipeline.py` (`_safe_fetch`); `tests/test_investigation.py` (`test_an_outage_with_nothing_drafted_is_incomplete_never_an_all_clear`).

## D-007 - Search the typology documents with local embeddings in ChromaDB
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: Ground the typology judgement in written guidance instead of the model's memory.
- Options considered:
1. Local embeddings (`all-MiniLM-L6-v2`) in an embedded ChromaDB - good: no API key, no per-call cost / bad: a roughly 80 MB model to load on each job
2. Put all documents into the prompt - UNKNOWN (not mentioned in the repo)
3. A hosted vector database - UNKNOWN (not mentioned in the repo)
- Decision: Option 1. The index is rebuilt inside each investigate job.
- Why: The "no API key, no per-call cost" reason is in `retrieval/store.py`. Why not the other options: UNKNOWN.
- Trade-off we accepted: Each job rebuilds the index and loads the embedding model (slow start).
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `retrieval/store.py`, `retrieval/chunking.py`, `dbx/jobs.py` (`build_index()` call), `databricks/requirements.txt`.

## D-008 - Search only the knowledge parts of each typology document
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: The folder holds more than red flags: detector notes, source lists, investigator questions and a writing standard.
- Options considered:
1. Index and search everything - bad: the detector notes quote this project's own accounts back at the matcher (circular), source lists carry no knowledge, and questions are guidance, not evidence of a match
2. Tag each piece (`typology`, `standard`, `investigation`, `supporting`) and let the typology agent search only `typology`
- Decision: Option 2. The narrative agent reads investigator questions directly from the files (`retrieval/questions.py`) instead of searching for them.
- Why: The reasons above are written in `retrieval/store.py` and `typology_docs/README.md`.
- Trade-off we accepted: UNKNOWN
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `retrieval/store.py`, `retrieval/questions.py`, `typology_docs/README.md`; `tests/test_retrieval_store.py`, `tests/test_questions.py`.

## D-009 - Retrieval distance is only a noise filter
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: Could a distance cut-off decide "match or no match"?
- Options considered:
1. Use distance to decide - bad: measured on live data, the best passage for real matches sat at 0.42 to 0.46, but for a normal account it sat at 0.49
2. Use distance only to drop clearly irrelevant passages (cut-off 0.75, deliberately loose) and let the model decide
- Decision: Option 2 (`TypologyConfig.max_distance = 0.75`).
- Why: The measurement above (code comment).
- Trade-off we accepted: The model decides, so the code-side checks in D-002 matter more.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `agents/typology.py` (`TypologyConfig`).

## D-010 - A stronger model for judging, writing and fact-checking; a cheaper one for summarizing
- Date: Claude split by 2026-10-11 (exact date UNKNOWN); Gemini split 2026-10-12 (commit `7fd21ec`)
- Status: Accepted
- The problem: Which model should do each of the four model jobs?
- Options considered:
1. One cheap model for everything - bad: Claude Haiku 4.5 flagged 35% of true sentences as a fact-checker (80 of 228), and Gemini 3.5 Flash flagged 113 of 227
2. A stronger model for judging, writing and fact-checking, a cheaper one for the evidence summary - good: Claude Sonnet 5 flagged 3% (7 of 228) after its prompt was fixed; Gemini 3.1 Pro 1.3% (README)
- Decision: Option 2. Claude run: Haiku 4.5 summarizes, Sonnet 5 does the rest. Gemini run: 3.5 Flash summarizes, 3.1 Pro does the rest. Each step has its own setting (`LLM_MODEL`, `TYPOLOGY_MODEL`, `NARRATIVE_MODEL`, `FAITHFULNESS_MODEL`).
- Why: The planted-error test results above (`evaluation_results/phase5_report.md` for Claude; the Gemini planted-error numbers appear only in `README.md`).
- Trade-off we accepted: The strong model costs more and Gemini 3.1 Pro is slower. Measured on 21 accounts: Claude pipeline $0.88; Gemini pipeline $3.36 and about 153 seconds per account.
- Who decided: UNKNOWN
- Revisit when: Gemini 3.1 Pro is a preview model (README): Google can change or retire it.
- Evidence: `evaluation_results/phase5_report.md`, `evaluation_results/gemini_report.md`, `README.md`, `config.py`, `dbx/providers.py`, `databricks.yml`.

## D-011 - The fact check must quote proof, and code verifies the quote
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: A draft can use only correct numbers and still add an invented motive or relationship. Code that checks numbers cannot see meaning.
- Options considered:
1. Plain code checks only - bad: cannot judge meaning
2. A second model call that rules on every sentence, with "supported" valid only if its quote really appears in the evidence (otherwise the verdict becomes "unverified")
3. A third-party evaluation library (RAGAS) - UNKNOWN (not mentioned in the repo)
- Decision: Option 2, plus free exact checks (`consistency.py`: score, tier, prior-case count, typology named). Pass thresholds default to zero flagged sentences.
- Why: The judge "can itself be wrong or lazy, so it is not trusted either" (module docstring).
- Trade-off we accepted: One more model call per draft; false alarms (3% on Claude).
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `guardrails/faithfulness.py`, `guardrails/consistency.py`; `tests/test_guardrails.py`. See C-007.

## D-012 - Flagged sentences go to a human and are never edited automatically
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: The judge is not perfect: in `sample_runs/9_fact_check_flagged_ACC-L-00095.txt` its reason says "actually supported" while its verdict says contradicted.
- Options considered:
1. Auto-delete or rewrite flagged sentences - bad: nothing in code can catch a verdict that disagrees with its own reasoning
2. List flagged sentences for the investigator; the draft is still delivered
- Decision: Option 2 (a failed fact check is not fatal; `faithfulness_error` is recorded).
- Why: `evaluation_results/phase5_report.md` "Limits".
- Trade-off we accepted: The human has to read the flagged items.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `agents/investigation.py` (`_faithfulness_node`), `evaluation_results/phase5_report.md`.

## D-013 - A hash-chained, append-only audit trail
- Date: by 2026-10-11 (exact date UNKNOWN); moved to Delta tables 2026-10-11 (commit `0f06951`)
- Status: Accepted
- The problem: Show who did what to an investigation, and make later tampering detectable.
- Options considered:
1. A normal editable log - bad: UNKNOWN (not discussed in the repo)
2. Events written once, each carrying the hash of the one before; the Delta table is created with `delta.appendOnly = true` so Delta itself refuses UPDATE and DELETE
- Decision: Option 2. `verify_chain` finds an edit, deletion or reordering. The code says this is tamper-evidence, not tamper-proofing: someone with full control of the storage could rewrite the whole chain.
- Why: Stated in `service/audit.py` and `service/delta.py`.
- Trade-off we accepted: The limit above.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `service/audit.py`, `service/delta.py`, `databricks/pipelines/reporting.sql` (`audit_chain_links` checks the chain in SQL); `tests/test_service_audit.py`.

## D-014 - Job rules: run once, daily limit, one open run per account
- Date: by 2026-10-11 (exact date UNKNOWN); kept on Databricks 2026-10-11
- Status: Accepted
- The problem: Stop one paid run from starting twice, stop runaway spending, and stop double clicks.
- Options considered:
1. Rely on a cloud budget alert - bad: budgets only alert, they do not stop spending (stated in `service/jobs.py`)
2. Rules in plain code, independent of the database: a job moves QUEUED to RUNNING for exactly one caller; at most 30 investigations per UTC day (default); while an account has an open job a new request returns the open one (the lock lasts 10 minutes, or until the job finishes)
- Decision: Option 2.
- Why: The reasons above are in the `service/jobs.py` docstring.
- Trade-off we accepted: A run longer than 10 minutes loses its duplicate lock; the one-run-at-a-time rule (D-019) is the second guard.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `service/jobs.py` (`JobStore.submit`, `claim`), `dbx/jobs.py` (`--daily-limit` default 30), `databricks.yml` (`daily_limit` 30); `tests/test_service_jobs.py` (simultaneous requests create one job).

## D-015 - A decision is final, tied to the draft, and only a supervisor can approve
- Date: by 2026-10-11 (exact date UNKNOWN)
- Status: Accepted
- The problem: A reviewer must approve exactly the draft they read, and not everyone should be able to approve.
- Options considered:
1. Allow changing a decision, any listed person may approve - bad: UNKNOWN (not discussed in the repo)
2. One decision per draft; the decision stores the draft's SHA-256 hash; roles come from the `reviewers` table; only a supervisor approves; the reviewer is the real Databricks login
- Decision: Option 2. Approval means "approved as a draft for manual filing".
- Why: Stated in `service/jobs.py`, `dbx/review.py`, `README.md`. Why only a supervisor may approve: UNKNOWN.
- Trade-off we accepted: In the web page, the page sends the hash it displayed and the server refuses a mismatch. In the notebook and terminal tool the hash is read from the stored draft at decision time, so they do not detect a draft that changed after it was displayed.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `service/jobs.py` (`decide`), `dbx/review.py` (`decide`), `dbx/webapp.py` (decision route); `tests/test_webapp.py`, `tests/test_service_jobs.py`.

## D-016 - First deployment on Google Cloud
- Date: 2026-10-11 (commit `bbdd4c5`)
- Status: Replaced by D-017
- The problem: Run the system somewhere other than a laptop.
- Options considered:
1. Google Cloud: Cloud Run, Cloud Tasks, Firestore, a locked-down Cloud Storage bucket, Firebase Hosting, Terraform - chosen
2. UNKNOWN - the repository does not record which other clouds were compared. (A plan file kept outside the repository, on the owner's computer, may hold the comparison. Ask the owner whether it can be used as a source.)
- Decision: Google Cloud.
- Why: UNKNOWN - ask the owner.
- Trade-off we accepted: UNKNOWN
- Who decided: UNKNOWN
- Revisit when: Already replaced (D-017).
- Evidence: commit `bbdd4c5` message and its `README.md`, `docs/deploy.md`, `infra/`.

## D-017 - Run on Databricks as jobs and a pipeline
- Date: 2026-10-11 (commits `0f06951` and `448b768`)
- Status: Accepted (replaces D-016)
- The problem: UNKNOWN - the commit messages say what changed, not why.
- Options considered:
1. Keep running on Google Cloud - UNKNOWN why not
2. Databricks: one investigate job per account, a weekly canary job, a Lakeflow reporting pipeline, a review notebook, all as one Asset Bundle
- Decision: Option 2. The Cloud Run API and worker, Terraform, web page, Dockerfile and Google Cloud adapters were removed the same day; the shared rules (jobs, audit, `process_job`, Delta storage) were kept.
- Why: UNKNOWN - ask the owner.
- Trade-off we accepted: Databricks Free Edition limits (per README: serverless compute only, restricted outbound internet, at most 3 apps, an app stops after about 24 hours, a workspace may be deleted if inactive).
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: commits `0f06951`, `448b768`; `databricks.yml`; `README.md` "Known limits".

## D-018 - One investigation is one job run; the web page only starts it
- Date: jobs 2026-10-11 (`0f06951`); web page 2026-10-20 (`ff622c0`)
- Status: Accepted
- The problem: Where should the long, expensive pipeline run?
- Options considered:
1. Run it inside the web server - UNKNOWN (not discussed)
2. Run it as a Databricks job, and have the page call the Jobs API "run now" (`JobsApiLauncher`)
- Decision: Option 2. The page and the job screen share one code path, one daily limit and one audit trail.
- Why: Stated in the `dbx/webapp.py` docstring.
- Trade-off we accepted: A run takes several minutes before the first query because the job installs its libraries first (README).
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `dbx/webapp.py`, `dbx/jobs_api.py`, `databricks.yml`.

## D-019 - Only one investigate run at a time
- Date: 2026-10-11 (commit `0f06951`)
- Status: Accepted
- The problem: The Delta document store is not built for parallel writers.
- Options considered:
1. Allow parallel runs - bad: the store "relies on" one run at a time; a lost race is detected but not designed for
2. `max_concurrent_runs: 1` with queueing enabled
- Decision: Option 2.
- Why: Stated in `service/delta.py` and `databricks.yml`.
- Trade-off we accepted: Throughput. 5,000 customers has not been timed (Not measured yet).
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `databricks.yml`, `service/delta.py`, `docs/databricks.md` "Operations".

## D-020 - Jobs sign in as a service principal, not a personal token
- Date: by 2026-10-11 (first commit message mentions "via a service principal")
- Status: Accepted
- The problem: A personal access token "expires on a calendar and belongs to a person".
- Options considered:
1. Personal access token - bad: the reason above (stated in `ingestion/databricks_auth.py`)
2. OAuth machine-to-machine with a service principal: short-lived tokens, renewed automatically; the principal can read only the label-free view
- Decision: Option 2. A personal token is still used for deploying and for the terminal review tool.
- Why: The docstring above. The secret expires after 90 days and must be rotated (`docs/databricks.md`).
- Trade-off we accepted: A secret to rotate every 90 days.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `ingestion/databricks_auth.py`, `docs/databricks.md`; `tests/test_databricks_auth.py`.

## D-021 - Gemini on Vertex AI as the default model family
- Date: 2026-10-12 (commit `7fd21ec`)
- Status: Accepted
- The problem: Which model family should the deployed jobs call?
- Options considered:
1. Claude on Vertex AI - bad: Google had not granted quota to the project (README; `docs/deploy.md` in the first commit says new projects start at zero and Google may deny until the billing account is a paid one)
2. Claude through the Anthropic API - still supported (`LLM_PROVIDER=anthropic`); why it was not the default: UNKNOWN
3. Models hosted by Databricks - README: Free Edition has no frontier models
4. Gemini on Vertex AI - chosen
- Decision: Gemini 3.5 Flash and 3.1 Pro, location `global`.
- Why: Partly verified (options 1 and 3 above). The deciding reason: UNKNOWN - ask the owner.
- Trade-off we accepted: Gemini 3.1 Pro is a preview model, and it was slower and dearer in the 21-account test (D-010).
- Who decided: UNKNOWN
- Revisit when: Gemini 3.1 Pro is retired or changed.
- Evidence: commit `7fd21ec`; `agents/llm.py`; `README.md`; `docs/databricks.md`.

## D-022 - A keyless login to Google
- Date: 2026-10-12 (commit `7fd21ec`)
- Status: Accepted
- The problem: The jobs need to prove who they are to Google without a stored secret.
- Options considered:
1. A service-account key file - bad: the organisation's policy forbids them
2. A Vertex AI API key - bad: not accepted for a normal project
3. Keyless: the job's Databricks token is swapped through Google's Security Token Service (a workload identity pool) for a short-lived Google token that impersonates a service account holding only the Vertex AI User role
- Decision: Option 3 (`GCP_AUTH=databricks`). `gcloud`, `token` and `adc` modes exist for local use.
- Why: Reasons 1 and 2 are written in `docs/databricks.md`.
- Trade-off we accepted: More setup (one-time Google commands in `docs/databricks.md`).
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `agents/gcp_auth.py`, `docs/databricks.md`; `tests/test_gemini_provider.py`.

## D-023 - Show the model citations as a list, store them as a dictionary
- Date: 2026-10-12 (commit `7fd21ec`)
- Status: Accepted
- The problem: Gemini's function-calling schema cannot describe a free-form map, so the model saw an empty object and answered `{}`; the typology step then saw no evidence.
- Options considered:
1. Keep the dictionary - bad: it failed on Gemini
2. Show the model a list of `{claim, ids}` and convert it back; if citations still come back empty, derive them from the ids named in each summary section
- Decision: Option 2 (`Citations` type; `with_derived_citations`).
- Why: Stated in `agents/models.py` and the commit message.
- Trade-off we accepted: A conversion step and a fallback to maintain.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `agents/models.py`, `agents/evidence.py`. See C-012.

## D-024 - Reach Supabase through its IPv4 pooler address
- Date: 2026-10-11 (commit `0f06951`)
- Status: Accepted
- The problem: Supabase's direct host is IPv6-only, and Databricks serverless compute has no IPv6 route to it.
- Options considered:
1. The direct host - bad: unreachable from the jobs
2. The pooler address (IPv4); `put_databricks_secrets.py` converts the address and tries both pooler names with the real password
- Decision: Option 2.
- Why: Stated in `scripts/put_databricks_secrets.py` and `docs/databricks.md`.
- Trade-off we accepted: UNKNOWN
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: the files above.

## D-025 - Three ways to review a draft: notebook, terminal tool, web page
- Date: notebook 2026-10-11 (`0f06951`); terminal tool 2026-10-12 (`54cfefa`); web page 2026-10-20 (`ff622c0`)
- Status: Accepted
- The problem: A person must be able to read and decide on a draft.
- Options considered:
1. Review notebook that runs as the signed-in person
2. Terminal tool (`scripts/dbx_review.py`) - added "for when the Databricks web login is unreliable" (commit message)
3. A web page served as a Databricks App - sign-in comes from Databricks, roles from the `reviewers` table
4. The Firebase web page on Google Cloud - built in the first commit, removed 2026-10-11 with D-017
- Decision: Keep 1, 2 and 3. All use the same decision rules (`dbx/review.py`).
- Why: Reason for option 2 is in its commit message. Why a Databricks App rather than other web hosting: UNKNOWN.
- Trade-off we accepted: A Databricks personal token cannot sign in to an App, so the deployed page can only be tried from a signed-in browser; `run_local_app.py` exists for local testing.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `databricks/notebooks/review.py`, `scripts/dbx_review.py`, `dbx/webapp.py`, `scripts/run_local_app.py`; commits above.

## D-026 - Choose Gemini or Claude per run, and compare two drafts
- Date: 2026-10-20 (commit `5cb9162`)
- Status: Accepted
- The problem: UNKNOWN - the commit message says what was built (a `provider` job parameter, a Model drop-down including "Both, then compare", and a side-by-side view), not why.
- Options considered:
1. One global model setting - UNKNOWN whether considered
2. A per-run `provider` (`gemini` or `claude`); a Claude run overrides the model settings (`dbx/providers.py`) and each record notes which family wrote it
- Decision: Option 2.
- Why: UNKNOWN - ask the owner.
- Trade-off we accepted: A Claude run needs `ANTHROPIC_API_KEY` in the Databricks secret scope. If it is missing, the run fails with a clear message and Gemini runs are unaffected (`docs/databricks.md`).
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: commit `5cb9162`; `dbx/providers.py`, `dbx/jobs.py`, `dbx/webapp.py`; `tests/test_dbx.py`, `tests/test_webapp.py`.

## D-027 - A weekly quality check (the canary)
- Date: by 2026-10-11 (the first commit mentions a "scheduled canary"); on Databricks 2026-10-11 (`0f06951`)
- Status: Accepted
- The problem: Notice when quality drops after a model or prompt change.
- Options considered:
1. No scheduled check - UNKNOWN whether considered
2. A job every Monday 06:00 UTC on 7 accounts; it stores one row per account in `eval_runs` and fails if a tier or typology is wrong or fewer than 90% of draft sentences are supported
- Decision: Option 2. The 90% bar is explained in code: the baseline was 99% over 236 sentences, and a 7-account canary is small.
- Why: README: it "doubles as a heartbeat" because Free Edition may delete an inactive workspace.
- Trade-off we accepted: A run costs money (a spending cap applies, default 1.6 dollars in the bundle).
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `evaluation/metrics.py` (`MIN_SUPPORTED_RATE`), `evaluation/sinks.py`, `dbx/jobs.py` (`canary_main`), `databricks.yml`.

## D-028 - A reporting pipeline of summary tables
- Date: 2026-10-11 (commit `0f06951`)
- Status: Accepted
- The problem: Turn raw tables into tables people can chart.
- Options considered: UNKNOWN - the repository does not record alternatives.
- Decision: A Lakeflow declarative pipeline of four materialized views: `investigations_flat`, `investigations_daily`, `canary_trend`, `audit_chain_links`, refreshed daily at 07:00 UTC and after each canary.
- Why: The one-line purpose is in `reporting.sql`. No dashboard exists in the repository yet, so nothing displays these tables.
- Trade-off we accepted: UNKNOWN
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `databricks/pipelines/reporting.sql`, `databricks.yml`.

## D-029 - A public repository with a secret scan
- Date: 2026-10-11 (GitHub's "Initial commit" `2dadb89`; secret-scan allowlist commit `091955b`)
- Status: Accepted
- The problem: UNKNOWN
- Options considered: UNKNOWN - public versus private is not discussed in the repository.
- Decision: The repository is public (MIT licence). CI scans the whole history with gitleaks. One exception is allowed: the Firebase web key in `web/config.js`, which the config calls public by design.
- Why: UNKNOWN - ask the owner why the repository is public.
- Trade-off we accepted: That Firebase web key remains in the history of commit `bbdd4c5` although the file was later removed.
- Who decided: UNKNOWN
- Revisit when: UNKNOWN
- Evidence: `LICENSE`, `.gitleaks.toml`, `.github/workflows/ci.yml`, commit `091955b`.

## Challenge log

### C-001 - Structuring was missed by a short detection window
- Date: by 2026-10-11 (exact date UNKNOWN)
- Symptom: A real structuring account in the data was not caught.
- Cause: It spread its deposits about 52 days apart across three branches, outside a 7-day window.
- Fix: Widened the sliding window to 90 days.
- Lesson: Test thresholds on live-looking data, not only on neat samples.
- Related: D-003
- Evidence: `detection/structuring.py`, `typology_docs/structuring.md`

### C-002 - Models sometimes answered in the wrong shape
- Date: by 2026-10-11 (exact date UNKNOWN)
- Symptom: A list field arrived as one sentence or as JSON text, and a whole answer was sometimes wrapped around it. In one case the same prompt failed twice and then succeeded six times.
- Cause: UNKNOWN beyond "models occasionally slip" (stated in `invoke_structured`).
- Fix: Lenient list types (`StringList`, `JsonList`), and `invoke_structured` retries a schema mismatch once, then raises a recorded `ModelOutputError` instead of a stack trace. The error says what the model sent.
- Lesson: Plan for an unreliable answer shape.
- Related: D-002
- Evidence: `agents/llm.py`, `agents/models.py`, `evaluation_results/phase5_report.md` "What the evaluation changed" 3; `tests/test_llm_robustness.py`

### C-003 - A Claude model rejected "temperature 0"
- Date: by 2026-10-11 (exact date UNKNOWN)
- Symptom: A request error from Claude Sonnet 5.
- Cause: Newer models reject an explicit temperature (comment in `agents/llm.py`).
- Fix: Send a temperature only to models listed as accepting one; call any other model without it, "slightly more variable, but never broken".
- Lesson: Do not assume every model accepts the same settings.
- Related: D-010
- Evidence: `agents/llm.py` (`_TEMPERATURE_OK_PREFIXES`); `tests/test_llm_provider.py`

### C-004 - Sonnet 5 mis-filled a field named "body"
- Date: by 2026-10-11 (exact date UNKNOWN)
- Symptom: The model packed its whole answer into the field as a JSON string.
- Cause: UNKNOWN; the code comment only reports that it happened repeatedly and that a field named `facts` filled correctly.
- Fix: Renamed the field to `facts`.
- Lesson: A field's name can change how a model fills it.
- Related: D-010
- Evidence: `agents/models.py` (`NarrativeText.facts`)

### C-005 - The layering reason hid facts from the later agents
- Date: by 2026-10-11 (exact date UNKNOWN)
- Symptom: The typology step called a red flag "cannot be assessed" although the detector had established the fact.
- Cause: The reason text only said "crosses borders"; the route (and the negative case, "stays within one country") lived only in a field the later agents never read.
- Fix: Every fact the detector established now goes into the reason text, including the route (for example US to KY).
- Lesson: What downstream agents read is the text, not the fields.
- Related: D-004
- Evidence: `detection/layering.py` (comment above `facts`); `tests/test_detection.py`

### C-006 - A typology "match" that cited no supported red flag
- Date: by 2026-10-11 (exact date UNKNOWN)
- Symptom: A low-confidence hawala "match" on a layering account cost 5 points of typology precision in the evaluation.
- Cause: One shared detail (a country) matched while every other red flag was unassessable.
- Fix: `finalize_assessment` drops a match with no supported red flag, and the prompt says so.
- Lesson: Measuring on many accounts finds problems single runs hide.
- Related: D-002
- Evidence: `agents/typology.py`, `evaluation_results/phase5_report.md`

### C-007 - The test of the fact-checker had its own bug
- Date: by 2026-10-11 (exact date UNKNOWN)
- Symptom: The fact-checker seemed to miss 6 of 68 planted errors.
- Cause: For accounts whose profile says "not a PEP", the test planted "is not a politically exposed person", which is true. It was a bug in the test, not the judge.
- Fix: The test now recognises the several ways the evidence words a negative.
- Lesson: Always sanity-check the test itself.
- Related: D-011
- Evidence: `evaluation/judge_check.py` (`_NOT_PEP_RE` comment); `evaluation_results/phase5_report.md`

### C-008 - Claude on Vertex AI never got quota
- Date: 2026-10-11 or earlier (stated in the first commit's README and `docs/deploy.md`)
- Symptom: Claude calls on Vertex AI could not be used.
- Cause: Google had not granted quota to the project. The first-commit `docs/deploy.md` says new projects start at zero and Google may deny the request until the billing account is a full paid account; whether that was the cause here: UNKNOWN.
- Fix: Used the Anthropic API, and later Gemini (D-021).
- Lesson: Ask for model quota early.
- Related: D-021
- Evidence: commit `bbdd4c5` (`README.md`, `docs/deploy.md`); current `README.md`

### C-009 - Installing the packages took over 30 minutes
- Date: by 2026-10-11 (in the first commit's `Dockerfile`)
- Symptom: `pip install` in the cloud build ran for 30 minutes or more.
- Cause: pip's dependency resolver backtracked on this set of packages.
- Fix: Used `uv`, which resolves in seconds.
- Lesson: When an install is slow, try another resolver before waiting.
- Related: D-016
- Evidence: `git show bbdd4c5:Dockerfile` (comment above the `uv` line)

### C-010 - Databricks job settings: wrong host form, and success reported as failure
- Date: 2026-10-11 (commit `20cae21`)
- Symptom: Jobs could not reach the workspace; a job that finished correctly was shown as failed.
- Cause: The SQL connector wants the bare host name without `https://`; and Databricks reports `sys.exit(0)` as a failure (comment in `databricks/jobs/investigate.py`).
- Fix: Pass the bare host (bundle variable `server_hostname`) and exit only on a real error.
- Lesson: Test the first real run on the platform early.
- Related: D-017
- Evidence: commit `20cae21`; `databricks.yml`; `databricks/jobs/investigate.py`

### C-011 - Review notebook libraries clashed, and tests leaked settings
- Date: 2026-10-11 (commits `52d262e` and `6f2d9fe`)
- Symptom: The notebook failed to run; some tests changed the real environment.
- Cause: Notebook: the commit title says compatible libraries had to be installed; the exact clash is UNKNOWN beyond what the notebook's `%pip` line shows (older `pydantic` and `pydantic-settings` pinned; `httpx`, `tenacity`, `python-dotenv` added). Tests: the fixture's own comment says the job entry points copy secrets and settings into `os.environ`, so tests could leak into each other.
- Fix: Pinned libraries in the notebook's `%pip` line; each test in `tests/test_dbx.py` now gets its own copy of the environment (automatic `private_environment` fixture).
- Lesson: Tests must not touch the real environment.
- Related: D-025
- Evidence: commits above; `databricks/notebooks/review.py`; `tests/test_dbx.py`

### C-012 - Gemini returned empty citations
- Date: 2026-10-12 (commit `7fd21ec`)
- Symptom: The typology step saw no evidence ids.
- Cause: Gemini's schema cannot express a free-form map (D-023).
- Fix: The list form of citations and a derive-from-claims fallback.
- Lesson: A schema that works for one model family may not work for another.
- Related: D-023
- Evidence: `agents/models.py`, `agents/evidence.py`

### C-013 - The automated checks failed because a package was missing
- Date: 2026-10-12 (commit `b23fc9c`)
- Symptom: CI was failing.
- Cause: `google-auth`, which the keyless Gemini login needs, was missing from the core requirements.
- Fix: Added it to `requirements-core.txt`.
- Lesson: Run the CI recipe in a clean environment before pushing.
- Related: D-022
- Evidence: commit `b23fc9c`; `requirements-core.txt`

### C-014 - A Gemini call hung for 28 minutes
- Date: 2026-10-12 (commit `8acee18`)
- Symptom: A job was held for 28 minutes.
- Cause: A model request had no deadline.
- Fix: Every Gemini call has a 300-second deadline and at most 2 retries.
- Lesson: Every outside call needs a deadline.
- Related: D-021
- Evidence: commit `8acee18`; `agents/llm.py` (`GEMINI_TIMEOUT_SECONDS`, `GEMINI_MAX_RETRIES`)

### C-015 - The Databricks web login was unreliable
- Date: 2026-10-12 (commit `54cfefa`)
- Symptom: Reviewing a draft through Databricks' own web screens was unreliable.
- Cause: UNKNOWN beyond the commit message ("when the Databricks web login is unreliable").
- Fix: Added `scripts/dbx_review.py`, a terminal tool that runs the same review logic.
- Lesson: Keep a second way to do a critical step.
- Related: D-025
- Evidence: commit `54cfefa`; `scripts/dbx_review.py`

### C-016 - The notebook report was cut at 4,000 characters
- Date: 2026-10-12 (commit `4a22940`)
- Symptom: The whole report did not show.
- Cause: `dbutils.notebook.exit` returns at most 4,000 characters (comment in the notebook).
- Fix: Print the full report; return the truncated copy only when `return_result` is "yes".
- Lesson: Know the limits of the platform's return channel.
- Related: D-025
- Evidence: commit `4a22940`; `databricks/notebooks/review.py`

### C-017 - Temporary outages of the data sources
- Date: by 2026-10-11 (exact date UNKNOWN)
- Symptom: A Databricks warehouse waking up returned a 400 "try again later"; Salesforce once returned a 404 on its query endpoint "intermittently".
- Cause: Temporary conditions of the outside systems (comments in `ingestion/retry.py`).
- Fix: Retries only for errors known to be temporary, with growing waits; never for Salesforce login failures (a repeated bad login can lock the user). A source that still fails becomes a recorded gap (D-006).
- Lesson: Separate temporary errors from permanent ones.
- Related: D-006
- Evidence: `ingestion/retry.py`; `tests/test_retry.py`

### C-018 - Firebase web key flagged by the secret scan
- Date: 2026-10-11 (commit `091955b`)
- Symptom: The secret scan objected to a key in `web/config.js`.
- Cause: A Firebase web key is meant to be public.
- Fix: Allow-listed that one file in `.gitleaks.toml`.
- Lesson: Document every scan exception.
- Related: D-029
- Evidence: commit `091955b`; `.gitleaks.toml`

### C-019 - Cloud deployment pipeline permission failures
- Date: 2026-10-11 (commit `53dc720`)
- Symptom: The deploy pipeline could not submit builds.
- Cause: The deploy account could not act as the build account, and gcloud lists the project's buckets, which needed its own permission.
- Fix: Builds run as a dedicated build account; the deployer may act as it, list buckets (custom role) and read the build bucket.
- Lesson: Check the minimum permissions a tool needs before narrowing them.
- Related: D-016 (that setup was later removed, D-017)
- Evidence: commit `53dc720` message

### C-020 - Documentation drifted from the code (found in this review)
- Date: 2026-11-19 (found while writing this file; not from git)
- Symptom: Several documents no longer match the code.
- Cause: The system moved from Google Cloud to Databricks (D-017) and from Claude to Gemini (D-021) and some text was not updated.
- Fix: NOT FIXED (this task changes no code). Details are in FLOW.md section 7.
- Lesson: Update docs in the same task as the code.
- Related: D-017, D-021
- Evidence: `README.md` says 550 tests (actual 577); several code docstrings still mention Cloud Run, Firestore, Cloud Storage or Claude; `docs/phase0-checklist.md` is the Google Cloud checklist; the README cost row compares unlike things.

## Questions for the owner (every UNKNOWN)
Status on 2026-11-23: the owner answered "I do not know" to the date and "who decided" questions (1 and 2) and then asked that every remaining question be treated the same way. All items below therefore stay UNKNOWN. If anyone later finds a real answer (for example in old notes or a mentor's memory), update the entry and remove it from this list.
1. D-001 to D-015: what were the real dates of the early decisions, if you remember any? Who decided each (you, a mentor, a client)?
2. D-003: why a floor of $7,000 and a minimum of 3 deposits? D-004: why 4 hops, 30 accounts, 500 rows?
3. D-007: were other ways to search the documents considered (all documents in the prompt, a hosted vector database)? Why local?
4. D-010: who chose the model split and was a single model considered for the whole chain?
5. D-011: was an evaluation library such as RAGAS considered? (The Phase 5 report mentions none.)
6. D-015: why can only a supervisor approve?
7. D-016: which other clouds were compared and why Google Cloud? May the deployment plan file kept outside the repository be used as a source?
8. D-017: why move from Google Cloud to Databricks?
9. D-021: why Gemini as the default rather than Claude through the Anthropic API?
10. D-025: why a Databricks App rather than other web hosting?
11. D-026: why a per-run model choice and a compare view?
12. D-027: why a weekly canary on 7 accounts, and why a 90% bar?
13. D-029: why is the repository public?
14. How were the 99,502 transactions and 47,475 case records generated? Only the Salesforce profile generator (`generate_salesforce_customers.py`) and the high-risk seed script are in the repository.
15. README results table: the Claude cost row ($2.63) is the whole evaluation (pipeline $0.88 + judge tests $1.30 + fact checking $0.46), while the Gemini cost row ($3.36) is the pipeline run alone. Which should the README show? (A like-for-like pipeline-only comparison is $0.88 for Claude and $3.36 for Gemini.) The Claude "about 30 seconds per account" figure appears only in the README, with no report file behind it.
