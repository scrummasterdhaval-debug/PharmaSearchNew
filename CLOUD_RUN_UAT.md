# PharmaSearch Cloud Run UAT

Cloud Run provides a permanent public HTTPS URL that remains available when the
developer computer is switched off. The root `Dockerfile` builds the FastAPI
backend and installs Chromium for regulator connectors that require Playwright.

## Prerequisites

1. Create or select a Google Cloud project with billing enabled.
2. Install the Google Cloud CLI.
3. From PowerShell, sign in and select the project:

```powershell
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

## Deploy

Run this command from the repository root:

```powershell
gcloud run deploy pharmasearch-uat --source . --region asia-south1 --allow-unauthenticated --memory 2Gi --cpu 1 --timeout 300 --concurrency 20 --min 1 --max 3 --port 8080
```

Cloud Run prints the permanent service URL after deployment. New deployments
create revisions, so a failed release can be rolled back from the Cloud Run
Revisions page.

## Verify Before UAT

Replace `YOUR_CLOUD_RUN_URL` with the generated HTTPS URL:

```text
YOUR_CLOUD_RUN_URL/api/version
YOUR_CLOUD_RUN_URL/api/data_health
YOUR_CLOUD_RUN_URL/
```

Acceptance checks:

- `/api/version` returns the current PharmaSearch build identifier.
- `/api/data_health` reports that the seed file exists and product count exceeds 8,000.
- The home page opens and accepts any active-substance name.
- A metformin search returns product records and header filters.
- Excel export downloads successfully on the tester's machine.

## Persistence Note

The current UAT image initializes SQLite from
`Backend/data/product_details_seed.jsonl`. Cloud Run's writable filesystem is
ephemeral, so cached searches and background enrichment can be lost when a new
instance or revision starts. The seeded regulatory dataset remains available on
every start. Production should move mutable data to PostgreSQL or another
managed database.
