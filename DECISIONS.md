# DECISIONS.md: what we chose, why, what we rejected, and what broke

Use this for "why X instead of Y?", "what was your biggest challenge?" and "what would you change?". Dates are 2026. Numbers are from the project's own runs and reports; where something was not measured, it says so.

## Decisions

| # | When | Decision | Why | Rejected or not chosen |
|---|---|---|---|---|
| 1 | Sep | **Rule-based detectors and score, no trained ML** | An investigator must be able to read why an account scored 70; no real labels exist to train on; the synthetic label column would leak the answer | An ML classifier. Revisit only with real data and a fair way to judge it |
| 2 | Sep 28 | **Structuring window of 90 days, not 7** | Live data showed a real structuring account spreading 3 deposits over 52 days across 3 branches, exactly to dodge a short window | The textbook 7-day window |
| 3 | Sep | **Network expansion: up to 4 hops, 30 accounts, 500 rows, wires only; a cap hit warns and marks evidence incomplete** | Layering chains are invisible to a one-account query, but an unbounded crawl costs time and money | Per-account queries only; unlimited graph crawl; silent truncation |
| 4 | Sep 22 | **Row-level security on Supabase** | A security alert showed `case_history` was readable by anyone with the public key. Our code connects as the table owner, so nothing broke | Leaving it open |
| 5 | Sep | **Never read the ground-truth label; read a view that omits it** | Agents must not see the answer. Enforced by a Unity Catalog view `transactions_safe` and checked live (the label and base table are refused to the job identity) | Trusting code never to select the column |
| 6 | Oct 1 | **Local embeddings (`all-MiniLM-L6-v2`) and embedded ChromaDB** | No API cost, no extra service, runs inside the job | Hosted vector databases (a managed search service would have cost more than the whole project) |
| 7 | Oct 1 | **Typology documents only from real regulatory sources (FinCEN, FFIEC, FATF)** | The knowledge base must be defensible. A 2008 SEC broker-dealer tool was evaluated and left out: wrong genre, wrong industry, 18 years stale | Padding the corpus with generic or invented material |
| 8 | Oct 4 | **The model writes, plain code checks** | Models vary run to run even at temperature 0. So code drops matches citing unretrieved evidence, sets `detector_corroborated` itself, and drops narrative sentences with figures, dates or ids not in the evidence | Trusting the model's own self-report |
| 9 | Oct 4 | **Retrieval distance is only a noise filter (`max_distance` 0.75)** | Real matches scored 0.42 to 0.46 and a normal account scored 0.49, so distance cannot decide a match | Using a distance cut-off as the match rule |
| 10 | Oct 4 | **A stronger model for judging and writing, a cheap one for summarizing** | 30 paired calls: Haiku 4.5 once over-claimed a red flag that code could not catch; Sonnet 5 did not. Cost is cents per run either way | One cheap model for every step |
| 11 | Oct 5 | **Fact-check must quote evidence and code verifies the quote; judge model chosen by test** | On planted errors Sonnet 5 caught 68 of 68 with 3% false alarms; Haiku flagged 35% of true sentences. Gemini 3.5 Flash caught all 69 planted errors but flagged 113 of 227 true sentences, so it only summarizes | RAGAS (an import bug against our LangChain version); a model grading itself without a code check |
| 12 | Oct 5 | **Flagged sentences go to a human, never auto-edited** | The judge sometimes contradicts its own reason text | Letting the system rewrite flagged sentences |
| 13 | Oct 5 | **Added 3 high-risk demo accounts (`ACC-H-*`) additively** | A HIGH tier needs both patterns (50 points each, high from 70); the data only had medium and low | Editing existing accounts. The seed script is dry-run by default and can remove exactly what it added |
| 14 | Oct 5 | **A source outage is `incomplete`, never `no_action`** | In AML, "we could not check" must never read as "all clear" | Treating missing data as no data |
| 15 | Oct 11 | **First deployment on Google Cloud (Cloud Run, Cloud Tasks, Firestore)** | Cheapest of AWS, GCP and Azure in the cost comparison (about $4 to $9 a month estimated) | AWS and Azure; background threads (Cloud Run throttles CPU after the response) |
| 16 | Oct 12 | **Moved the whole runtime to Databricks jobs and a Lakeflow pipeline, then tore down Google Cloud** | The owner asked for jobs and pipelines on Databricks. GCP was removed only after the Databricks run matched it | Keeping two runtimes |
| 17 | Oct 12 | **Gemini on Vertex AI as the default, Claude still selectable** | The owner asked for no Claude API. Gemini works on default quota, while Google denied Claude quota on this billing account | Claude on Vertex AI (blocked by quota) |
| 18 | Oct 12 | **Keyless Google login (workload identity federation)** | Organisation policy forbids service-account key files and Vertex API keys are not accepted for project use. The Databricks service principal token is swapped for a Google token, so no secret is stored | Key files; API keys |
| 19 | Oct 12 | **Service principal with OAuth for jobs; a personal token only for deploys** | The first personal token expired and broke a run. The principal reads only the label-free view | A personal token in the jobs |
| 20 | Oct 12 | **Append-only Delta audit with a hash chain; optimistic versioned writes; one run at a time** | Delta refuses UPDATE and DELETE on the audit table; each event carries the previous hash; the stores are not built for parallel runs | A mutable log; parallel runs |
| 21 | Oct 12 | **Public GitHub repo with CI and a secret scan** | The data is synthetic and the history is scanned. A restricted Firebase web key remained in old history | A private repo |
| 22 | Oct 20 | **Web page as a Databricks App; the notebook and terminal tool stay** | Databricks signs the user in, so the reviewer is a real login. The web login is sometimes unreliable, so the terminal tool is the fallback | A custom login system |
| 23 | Oct 20 | **Per-run Gemini or Claude choice and a side-by-side compare** | Lets the owner see both models on the same account. A Claude run overrides the model settings through `dbx/providers.py` | A global switch |

## What broke, and what we did about it

| When | What broke | Cause | Fix |
|---|---|---|---|
| Sep | Salesforce login failed | The org needed the SOAP login setting and a permission set | Enabled both; the chat connector was broken platform-side, so the code uses `simple-salesforce` |
| Oct 4 | Evidence agent crashed on a model answer | The model returned a bare string where a list was expected | Lenient list types and `invoke_structured`, which turns a schema slip into a recorded error and retries once |
| Oct 4 | Typology step 400 error | Sonnet 5 rejects `temperature=0` | Send temperature only to models documented to accept it |
| Oct 4 | The model called the layering flag "unassessable" | The reason text only said "crosses borders" | Reason now names the route (US to KY) and the prompt shows country and branch |
| Oct 5 | Sonnet 5 filled a field named `body` with JSON or placeholders | Field name confused the tool call | Renamed it `facts` |
| Oct 5 | The fact-checker looked worse than it was | A bug in my planted-error test (it said "not a PEP" where that was true) | Fixed the test; lesson: always sanity-check the test itself |
| Oct 11 | `pip install` took over 30 minutes in the cloud build | Dependency backtracking | Used `uv`, about 20 seconds |
| Oct 11 | A probe printed a secret in the tool output | Careless debugging | Rotated the secret with the owner's approval; secrets are handled silently since |
| Oct 12 | Databricks bundle variable was empty | `${workspace.host}` is not set in this setup | Added a bundle variable `server_hostname` |
| Oct 12 | Jobs were marked failed after succeeding | `sys.exit(0)` is reported as a failure by Databricks | Job scripts exit only on a real error |
| Oct 12 | Citations came back empty on Gemini | Gemini's schema has no free-form maps, so the typology step saw no evidence | Citations are shown as a list of claim and ids and converted back, with a fallback that derives them |
| Oct 12 | CI went red | Removed `google-auth` from the core requirements | Put it back and verified in a fresh environment |
| Oct 12 | One Gemini call hung for 28 minutes | No deadline on the call | 300 second timeout and 2 retries on every call |
| Oct 20 | Review notebook: "non-SQL cells will fail" | Not on Serverless compute | Switch compute to Serverless |
| Oct 20 | Review notebook import errors | The runtime's `typing_extensions` shadows the newer pydantic; `httpx` missing | Pinned older pydantic in the notebook's `%pip` line and added the missing packages |
| Oct 20 | Notebook report cut at 4,000 characters | `dbutils.notebook.exit` limit | Print the whole report; return a truncated copy only when asked |
| Oct 20 | I could not test the deployed page from here | Personal tokens do not authenticate to Databricks Apps | Test locally with `scripts/run_local_app.py`; the owner opened the deployed page and confirmed it works |

## The biggest challenge

Keeping a language model honest. Every model step needed a matching code-side check, and the checks themselves needed tests, because the first version of the fact-check test had its own bug. Close second: getting Gemini and Databricks to work together under Free Edition limits (restricted internet, no key files, no Claude models).

## What I would change

- Add more detectors. Only structuring and layering have rules; the other five typologies are retrieval-only.
- Measure the value: time saved against a manual investigation is not measured yet, and neither is behaviour on real data or under load.
- Build a manager dashboard on the existing reporting tables, and a guided demo mode for non-technical viewers.
- Score jurisdiction risk: the synthetic layering hops land in an offshore country, and no detector scores that.
- Move off the deprecated `ChatVertexAI` client and off the preview Gemini model when a stable one fits.
- Reduce start-up time: every job installs its libraries first, so a run takes minutes before the first query.

## Not decided yet

- Whether a trained model should ever sit beside the rules, and how to judge it without real labels.
- Which model should be the default for a pilot (Gemini is slower and dearer here: about 150 s and $3.36 per 21 accounts, against about 30 s and $2.63 for Claude).
- What a production version needs first: identity and roles, monitoring, data protection or validation.
