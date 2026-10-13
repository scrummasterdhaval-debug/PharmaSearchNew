# Putting PharmaSearch on your own domain

This is the whole deployment: one server, HTTPS, a sign-in in front of every
page, the database and the register copies on a disk that survives updates, and
a nightly backup.

## What the server needs

| | |
|---|---|
| CPU and memory | 2 cores, 4 GB. Indexing a regulator's file is the heaviest moment. |
| Disk | 40 GB. The register copies come to about 6 GB today and grow as markets are added; the rest is room for backups and updates. |
| System | Any Linux with Docker and the Compose plugin. |
| Open ports | 80 and 443 only. |
| Cost | About €10–20 a month at Hetzner, DigitalOcean or Linode. |

Nothing else is needed: certificates, renewals and restarts are handled by the
setup below.

## 1. Point the domain at the server

Add one DNS record with whoever manages the domain:

```text
A    search.example.com    <the server's IP address>
```

Wait until `ping search.example.com` answers from that address. The certificate
cannot be issued before it does.

## 2. Put the code on the server

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER" && newgrp docker
git clone https://github.com/<your-account>/PharmaSearchLatest.git pharmasearch
cd pharmasearch
```

## 3. Write the settings

```bash
cp .env.example .env
python3 -c "import secrets; print('PHARMASEARCH_SESSION_SECRET=' + secrets.token_urlsafe(48))"
docker run --rm -it -v "$PWD/Backend:/app" -w /app python:3.12-slim \
    python tools/make_admin_password.py <your-username>
```

Open `.env` and fill in:

- `PHARMASEARCH_DOMAIN` — the address from step 1.
- `TLS_EMAIL` — where Let's Encrypt writes about the certificate.
- `PHARMASEARCH_ADMIN_USERNAME` and `PHARMASEARCH_ADMIN_PASSWORD_HASH` — from
  the command above. **Choose the password yourself and keep it in a password
  manager; only its hash belongs in this file.**
- `PHARMASEARCH_SESSION_SECRET` — the line printed above.

`.env` is git-ignored. Anyone who reads it can sign in as the admin, so keep it
off email and chat.

## 4. Start it

```bash
docker compose up -d --build
docker compose logs -f app
```

The first start downloads the regulators' files in the background: the FDA,
Health Canada, Italy, Brazil, Japan, Singapore, Ukraine, Ireland, Taiwan and
Russia take about 20 minutes together, Saudi Arabia about 9, and Taiwan's
English company names over an hour. Searches work throughout; a register that
is not ready yet says so rather than reporting no products.

Then open `https://search.example.com`. The sign-in page is the only thing a
visitor without a session ever sees.

## 5. Check it

```bash
curl -I https://search.example.com/healthz          # 200, and the headers below
curl -I http://search.example.com/                  # 308 to https
curl -I https://search.example.com/api/search?substance=metformin   # 401 without a session
```

Every answer carries `Strict-Transport-Security`, `Content-Security-Policy`,
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and
`Referrer-Policy: no-referrer`.

## What protects it

- **Every page needs a session.** The searches, the exports and the JSON
  endpoints alike; `/healthz` and the sign-in page are the only exceptions.
- **Passwords are stored as a scrypt hash**, never in plain text, and a user
  name and address that fail five times wait fifteen minutes.
- **The session cookie** is signed, `HttpOnly`, `SameSite=Lax` and `Secure`.
- **The site answers only to its own domain** (`PHARMASEARCH_ALLOWED_HOSTS`), so
  it cannot be reached through another name pointed at the server.
- **`PHARMASEARCH_ENV=production` refuses to start** if the sign-in, the session
  secret or the host list is missing, and says which.
- **The container runs as a user without administrative rights** and writes only
  to the mounted disk.
- **The application is not published on any port**; only Caddy is reachable.

## Keeping the data current

The `jobs` container runs three jobs on one clock. Nothing has to be set up for
them; they start with the rest of the stack.

| When | Job | What it does |
|---|---|---|
| Every day 03:00 | `tools/refresh_registers.py` | Downloads any of the twelve published registers that is past its own cadence — a week for most, a month for Brazil. On a day when nothing is due it costs nothing. |
| Sunday 02:00 | `tools/refresh_live_sources.py` | Re-asks the regulators that are searched one molecule at a time — the UK, the EU national sites, Asia, the Middle East — about the molecules already stored, oldest first, within a five-hour budget. |
| Monday 10:00 | `tools/weekly_mhra_links.py` | Backs the database up, then replaces links to MHRA documents the regulator has deleted. |

Ask it what it has done, or run one job by hand:

```bash
docker compose exec jobs python tools/scheduled_jobs.py --status
docker compose exec jobs python tools/refresh_registers.py --status
docker compose exec jobs python tools/scheduled_jobs.py --run registers
```

Each job writes to `/data/logs` on the mounted disk: `refresh_registers.log`,
`refresh_live_sources.log`, `weekly_mhra_links.log` and `scheduled_jobs.log`.
A job whose moment passed while the server was down runs at the next check
rather than being skipped.

On Windows the same three run as scheduled tasks instead — "PharmaSearch daily
registers", "PharmaSearch weekly live sources" and "PharmaSearch weekly MHRA
links" — and the container is not involved.

## Updating

```bash
cd pharmasearch && git pull && docker compose up -d --build
```

The database, the register copies and the exports are on the `pharmasearch-data`
volume and are untouched by a rebuild. Roll back with
`git checkout <previous-commit> && docker compose up -d --build`.

## Backups

A copy of the database is written to `./backups` every night and the last
thirty are kept. Copy them off the server as well:

```bash
rsync -az server:/home/you/pharmasearch/backups/ ./pharmasearch-backups/
```

To restore, stop the app, put the chosen file in place of the database on the
volume, and start it again:

```bash
docker compose stop app
docker run --rm -v pharmasearch_pharmasearch-data:/data -v "$PWD/backups:/backups" alpine:3 \
    cp /backups/pharmasearch-20260918-0300.db /data/pharmasearch.db
docker compose start app
```

The register copies need no backup: they are regulators' own files and rebuild
themselves.

## Two people signing in

The application has one admin account today. Until it has more, give the second
person their own account by running a second copy with its own `.env` and
domain, or ask for per-user accounts to be built — a day's work, and the place
for it is `services/admin_auth.py`.

## Somewhere other than one server

- **Google Cloud Run**: `CLOUD_RUN_UAT.md`. Mount a volume for `/data` and set
  the same variables, or the registers download again on every cold start.
- **Render or Railway**: the files in this repository work, but only on a paid
  plan with a persistent disk mounted at `/data`. A free plan wipes the disk,
  which loses the saved products and re-downloads 6 GB.
