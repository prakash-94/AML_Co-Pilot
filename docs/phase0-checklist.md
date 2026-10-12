# Phase 0 checklist - the parts only you can do

Everything here needs *your* Google and Databricks accounts, so I cannot do it from the build
session. Run the commands in **your own terminal**. Never paste a token, key or password into the
chat - when something needs a secret, you type it into the tool that asks for it.

Cost of this whole checklist: a few cents (one tiny Claude call). Nothing here creates anything
that bills while idle.

Fill in the **Findings** table at the bottom as you go, then send me the table (it contains no secrets).

## A. Google Cloud project, billing and a budget alert (do this FIRST)

```bash
gcloud auth login
gcloud billing accounts list                       # note the ACCOUNT_ID of your billing account

# Choose a globally unique id, e.g. aml-copilot-<your-initials>-<4 digits>
gcloud projects create PROJECT_ID --name="AML Copilot"
gcloud billing projects link PROJECT_ID --billing-account=ACCOUNT_ID
gcloud config set project PROJECT_ID
```

Budget alert at $15 a month (it **emails you; it does not stop spending** - budgets never cap):

```bash
gcloud billing budgets create \
  --billing-account=ACCOUNT_ID \
  --display-name="aml-copilot-15usd" \
  --budget-amount=15USD \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.8 --threshold-rule=percent=1.0
```

Turn on the services the deployment will use (free to enable):

```bash
gcloud services enable \
  run.googleapis.com cloudtasks.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com aiplatform.googleapis.com \
  identitytoolkit.googleapis.com cloudscheduler.googleapis.com firebase.googleapis.com \
  firebasehosting.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
```

## B. Claude on Vertex AI - start now, quota can take days

1. Google Cloud console -> **Vertex AI -> Model Garden**. Search "Claude".
2. Open **Claude Sonnet 5**, click **Enable** (accept the terms). Do the same for **Claude Haiku 4.5**.
3. Note the **model id** and the **endpoint types** each card shows (expected: `claude-sonnet-5` on
   the *global* endpoint; Haiku 4.5 as `claude-haiku-4-5@20251001`).
4. Console -> **IAM & Admin -> Quotas**, filter by `base_model` = each Claude model and location
   `global`; if the limit is 0 (or very low), **request an increase** (e.g. 60 requests/minute and
   200,000 tokens/minute is far more than this project needs).
5. Test one tiny call (replace PROJECT_ID; this costs well under a cent):

```bash
curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/PROJECT_ID/locations/global/publishers/anthropic/models/claude-sonnet-5:rawPredict" \
  -d '{"anthropic_version":"vertex-2023-10-16","max_tokens":20,"messages":[{"role":"user","content":"Reply with the word OK"}]}'
```

If the URL differs on the model card, use the card's version and tell me.

## C. Firebase (hosting + sign-in) and the paid plan

1. https://console.firebase.google.com -> **Add project** -> choose **your existing PROJECT_ID**.
2. Upgrade to the **Blaze (pay-as-you-go)** plan. It has no monthly fee; free quotas still apply, and
   the $15 budget alert above covers it. (Cloud Run behind Firebase Hosting very likely needs Blaze -
   if the console lets you create a Cloud Run rewrite on Spark, tell me.)

## D. Databricks Free Edition - what does your workspace allow?

Open your workspace and try each item. Record **works / blocked / not found** and any error text.

| # | Test | How |
|---|---|---|
| 1 | Unity Catalog + our table | SQL editor: `SELECT current_metastore();` then `DESCRIBE EXTENDED aml_copilot.transactions;` |
| 2 | Create a view | `CREATE VIEW aml_copilot.transactions_safe AS SELECT transaction_id, account_id, timestamp, amount, type, counterparty_account, branch_id, country FROM aml_copilot.transactions;` then `SELECT COUNT(*) FROM aml_copilot.transactions_safe;` |
| 3 | Service principal | Settings (your avatar) -> **Identity and access** -> **Service principals** -> *Add*. Can you create one? |
| 4 | OAuth secret for it | On the service principal -> **Secrets** -> *Generate secret* (shown once - store it only in a password manager) |
| 5 | Grant to the principal | `GRANT SELECT ON VIEW aml_copilot.transactions_safe TO ` followed by the principal's application id in backticks |
| 6 | Secret scopes | CLI: `databricks secrets create-scope aml-test` (then delete it) |
| 7 | MLflow | Left menu **Experiments** -> create one and log a metric from a notebook |
| 8 | Outbound internet from a job | Run a notebook cell: `import urllib.request; print(urllib.request.urlopen("https://www.salesforce.com", timeout=10).status)` and the same for your Supabase host and `https://aiplatform.googleapis.com` |

Why each matters: 1-2 = the view that hides `ground_truth_label`; 3-5 = replacing the expiring token;
6-7 = optional extras; 8 = decides whether the scheduled evaluation can run inside Databricks (the
plan assumes it cannot, and runs it on Google Cloud instead).

## E. A private GitHub repository

Create an empty **private** repository (no README, no .gitignore). Tell me its URL, nothing else.

---

## Findings (fill in, then send me this table)

| Item | Result |
|---|---|
| GCP project id | |
| Billing linked + budget alert created | yes / no |
| Sonnet 5 enabled; model id shown on card | |
| Haiku 4.5 enabled; model id shown on card | |
| Vertex quota (RPM / TPM) for each model, or "had to request" | |
| Tiny Vertex call worked (copy the `"text"` field only) | |
| Firebase project created; Blaze accepted | yes / no |
| D1 Unity Catalog / table readable | |
| D2 View created | |
| D3 Service principal creatable | |
| D4 OAuth secret creatable | |
| D5 Grant on view works | |
| D6 Secret scopes | |
| D7 MLflow | |
| D8 Outbound internet (salesforce / supabase / googleapis) | |
| GitHub repository URL | |
