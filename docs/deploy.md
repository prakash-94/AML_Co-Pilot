# Deploying and operating on Google Cloud

```bash
PROJECT=project-f6f56c9a-dbb2-4525-8f7
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)     # Terraform uses your gcloud login
```

## First-time setup (already done for this project)
1. `cd infra && terraform init -backend-config=backend.hcl && terraform apply` - registry, Firestore, audit bucket,
   secret slots, queue, service accounts, alerts. State lives in `gs://<project>-tfstate`.
2. `python scripts/put_secrets.py --project $PROJECT` - copies the secrets from `.env` to Secret Manager.
3. Build an image, then `terraform apply -var deploy_services=true -var image_tag=<tag>` for the first services.
4. `python scripts/create_user.py --project $PROJECT --email you@example.com --role supervisor` (asks for a password).
5. `python scripts/deploy_hosting.py --project $PROJECT` - publishes `web/` (needs no `firebase login`).

## Shipping a change
```bash
TAG=$(git rev-parse --short HEAD)
IMAGE=us-central1-docker.pkg.dev/$PROJECT/aml/aml-copilot:$TAG
gcloud builds submit --config cloudbuild.yaml --project $PROJECT --substitutions=_IMAGE=$IMAGE,_GIT_SHA=$TAG .
for svc in aml-worker aml-api; do gcloud run services update $svc --region us-central1 --project $PROJECT --image $IMAGE; done
gcloud run jobs update aml-eval --region us-central1 --project $PROJECT --image $IMAGE
curl https://<api-url>/status          # shows the build sha
```
The same steps run from GitHub Actions (`.github/workflows/deploy.yml`) once `terraform apply -var github_repo=owner/repo`
has created the keyless login. Rollback = run the update commands with an older tag.

## Switching Claude to Vertex AI
Vertex needs quota per model; new projects start at 0 and Google may deny the request until the billing
account is a full (paid) account. Check with:
```bash
curl -s -X POST "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/global/publishers/anthropic/models/claude-haiku-4-5:rawPredict" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" \
  -d '{"anthropic_version":"vertex-2023-10-16","max_tokens":10,"messages":[{"role":"user","content":"Say OK"}]}'
```
A JSON answer means quota exists; `RESOURCE_EXHAUSTED` means it does not. Then set `llm_provider = "vertex"` in
`infra/terraform.tfvars`, `terraform apply`, and re-run the canary (`gcloud run jobs execute aml-eval --wait`).
Before relying on it, repeat the Phase 5 comparison (tier 100%, typology 100/100, at least 230 of 236 sentences supported).

## Operations
* Weekly quality check: Mondays 06:00 UTC; results in Databricks table `workspace.aml_copilot.eval_runs`.
  Run it now: `gcloud run jobs execute aml-eval --region us-central1 --project $PROJECT --wait`.
* Rotate the Databricks service-principal secret every 90 days (it is stored as `DATABRICKS_CLIENT_SECRET`).
* Rotate Salesforce / Supabase / Anthropic secrets by `put_secrets.py` (adds a new version), then destroy old versions.
* Teardown: `terraform destroy`. The audit bucket cannot be deleted until its objects pass the 7-day retention.
