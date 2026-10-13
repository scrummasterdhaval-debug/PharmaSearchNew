Copyright (c) 2026 the author. All rights reserved.
Shared for job application and interview review only. Not licensed for
copying, use or redistribution without written permission.

PHARMASEARCH
============

PharmaSearch is a web application that searches national medicine regulators
for an active substance and shows, on one page, every registered product it
finds: product name, marketing-authorisation holder, manufacturer, strength,
dosage form, pack size, ATC code, registration number and dates, status, and
links to the official documents (SmPC, patient leaflet, assessment report).

Results can be exported to Excel. A signed-in admin area runs data harvests,
reviews manufacturer suggestions and watches the health of every connector.


1. WHAT IT COVERS
-----------------

56 regulator connectors, grouped by region:

  United States (4)   FDA, Drugs@FDA, FDA Orange Book, FDA Purple Book
  United Kingdom (1)  MHRA
  Europe (23)         EMA, EU MRI Product Index, AIFA Italy, ALIMS Serbia,
                      ANMDMR Romania, BDA Bulgaria, Belgium FAMHP,
                      BfArM Germany (view-only), CBG Netherlands,
                      Cyprus Pharmaceutical Services, DKMA Denmark,
                      DMP Norway, EOF Greece, France BDPM, HPRA Ireland,
                      Ireland medicines.ie, Malta Medicines Authority,
                      SUKL Czech Republic, SUKL Slovakia, Spain CIMA,
                      URPL Poland, Ukraine DRLZ, ZVA Latvia
  Switzerland (1)     Swissmedic
  Russia (1)          GRLS
  Canada (1)          Health Canada
  Latin America (2)   ANVISA Brazil, INVIMA Colombia
  Asia (12)           BPOM Indonesia, CDSCO India, DAV Vietnam,
                      FDA Philippines, HSA Singapore, Hong Kong Drug Office,
                      MFDS South Korea, NDDA Kazakhstan, NMPA China,
                      NPRA Malaysia, TFDA Taiwan, Thai FDA
  Japan (2)           MHLW Japan, PMDA Japan
  Middle East (4)     Israel Drug Registry, MoPH Lebanon, SFDA Saudi Arabia,
                      TITCK Turkey
  Africa (2)          FDA Ghana, SAHPRA South Africa
  Oceania (3)         TGA Australia, PBS Australia, Medsafe New Zealand

Some regulators publish a downloadable register (a spreadsheet, CSV or XML
file). Those are downloaded and indexed in the background, so a search reads
a local copy. The rest are queried live at search time. The connector status
page in the admin area shows which is which and whether each one is working.

Germany is view-only: BfArM's terms allow showing results but not storing or
exporting them. BFARM_REUSE_REQUEST.md and SWISSMEDIC_REUSE_REQUEST.md are
draft letters asking those regulators for wider reuse rights.


2. HOW IT WORKS
---------------

  Browser  ->  FastAPI app (Backend/main.py)
                 |
                 +-- sign-in middleware: every page needs an account;
                 |   admin-only pages need an admin account
                 |
                 +-- search pipeline (services/search_pipeline.py)
                 |     runs the selected connectors in parallel,
                 |     merges and de-duplicates rows by molecule,
                 |     fills missing fields from stored records
                 |
                 +-- connectors (sources/*.py, listed in
                 |   sources/source_registry.py)
                 |
                 +-- SQLite database (products, users, sessions, jobs)
                 |
                 +-- background jobs: register downloads, harvests,
                     evidence backfill, manufacturer lookups

Main folders:

  Backend/main.py            Web routes, sign-in middleware, pages
  Backend/admin_routes.py    Admin area routes
  Backend/sources/           One module per regulator, plus the registry
  Backend/services/          Search pipeline, harvesting, admin, email,
                             optional AI enrichment, security headers
  Backend/templates/         HTML pages (Jinja2)
  Backend/static/            CSS and scripts
  Backend/tools/             Command-line tools: admin password hash,
                             register refresh, scheduled jobs
  Backend/deploy/            Container entrypoint
  Backend/data/              Seed data shipped with the app
  Dockerfile, docker-compose.yml, Caddyfile
                             The production setup: the app and a Caddy
                             web server that handles HTTPS

Technology: Python 3.12, FastAPI, Uvicorn, Jinja2, pandas, openpyxl,
requests, BeautifulSoup, Playwright (for the few sites that need a real
browser), pypdf, SQLite, Docker, Caddy.


3. RUNNING IT LOCALLY
---------------------

  cd Backend
  python -m venv .venv
  .venv/bin/pip install -r requirements.txt          (Windows: .venv\Scripts\pip)
  .venv/bin/python -m playwright install chromium
  .venv/bin/python tools/make_admin_password.py <your-username>
      -> copy the printed hash into PHARMASEARCH_ADMIN_PASSWORD_HASH
  .venv/bin/uvicorn main:app --reload --port 8000

Then open http://localhost:8000 and sign in.


4. DEPLOYING IT
---------------

The production setup is one server running Docker Compose. DEPLOY.md has the
full steps; in short:

  1. Point a domain at the server.
  2. Copy the code to the server.
  3. cp .env.example .env and cp secrets.env.example secrets.env,
     then fill them in (see section 5).
  4. docker compose up -d --build

Caddy obtains and renews the HTTPS certificate automatically. On first start
the app downloads the regulators' register files in the background (about
20-30 minutes); searches work during that time.

CLOUD_RUN_UAT.md and railway.json describe older test deployments.


5. SETTINGS (ENVIRONMENT VARIABLES)
-----------------------------------

No password, key or secret is stored in this repository. Every one of them
is read from the environment. Keep them in .env / secrets.env on the server;
both files are git-ignored.

Required:
  PHARMASEARCH_ADMIN_USERNAME       Admin sign-in name
  PHARMASEARCH_ADMIN_PASSWORD_HASH  scrypt hash from tools/make_admin_password.py
  PHARMASEARCH_SESSION_SECRET       Signs session cookies (a long random string)
  PHARMASEARCH_DOMAIN               The site's address (Docker Compose)
  TLS_EMAIL                         Contact address for Let's Encrypt

Regulator API credentials (optional; the source is skipped when unset):
  MHRA_SEARCH_API_KEY               Query key for MHRA's product search index
                                    (United Kingdom)
  MEDICINES_IE_CLIENT_ID            OAuth client for the medicines.ie API
  MEDICINES_IE_CLIENT_SECRET          (Ireland)

Email (password resets and sign-up confirmation):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
  or BREVO_API_KEY
  FROM_EMAIL                        Sender address
  PHARMASEARCH_REQUIRE_EMAIL_CONFIRMATION

Optional AI enrichment (fills fields from documents; off unless enabled):
  PHARMASEARCH_AI_ENABLED, OPENAI_API_KEY, PHARMASEARCH_AI_MODEL,
  PHARMASEARCH_AI_TIMEOUT_SECONDS, PHARMASEARCH_AI_MAX_TEXT_CHARS

Other:
  PHARMASEARCH_BASE_URL             Address used in emailed links
  PHARMASEARCH_ALLOWED_HOSTS        Host names the app answers to
  PHARMASEARCH_DB_PATH              SQLite file location
  PHARMASEARCH_DATA_DIR             Data folder (registers, exports, backups)
  PHARMASEARCH_EXPORT_DIR, PHARMASEARCH_BACKUP_DIR, PHARMASEARCH_LOG_DIR
  PHARMASEARCH_LIVE_MAX_AGE_DAYS    How long a cached live result stays fresh
  PHARMASEARCH_ENV                  "production" turns on the strict checks


6. ACCOUNTS AND SECURITY
------------------------

- Every page except sign-in, registration, password reset and /healthz
  needs an account.
- Registered users can search. Everything else (catalogue, exports,
  connector pages, harvests, database reset) is admin-only.
- Passwords are stored as scrypt hashes. Session cookies are signed and
  HTTP-only. Security headers are set on every response.
- The database reset is admin-only and POST-only; the old GET /reset_db now
  just redirects to the admin area.


7. MAIN PAGES AND API
---------------------

  /                  Search page
  /login, /register, /forgot-password, /reset-password, /verify-email
  /admin             Admin area: dashboard, records, data, email,
                     manufacturer review, operations
  /catalogue         Everything stored, filterable (admin)
  /connector_status_page, /connector_health_page   Connector status (admin)
  /api/search        Search as JSON
  /api/sources       The connector list
  /search_jobs/...   Background searches and their Excel export
  /export/<substance>, /deep_export/<substance>    Excel exports
  /healthz           Health check for the server or load balancer


8. TESTS
--------

  cd Backend
  python -m pytest -q test_core.py test_access_roles.py test_admin_email.py

The tests run offline: regulator responses are mocked.


9. NOTES
--------

- The connectors read public regulator data. Each regulator's terms of reuse
  apply to what is shown and exported.
- A connector can stop working when a regulator changes its website. The
  connector status page is the first place to look.
