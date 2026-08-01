# Production deployment (AWS EC2 or Contabo VPS)

This project runs as one Docker Compose stack:

```text
Internet → Caddy (HTTPS) → FastAPI app → PostgreSQL
```

Only Caddy exposes ports `80` and `443`. PostgreSQL is on a private Docker
network and is never published to the internet.

## 1. Prepare the server

Use an Ubuntu/Debian VPS with a public IPv4 address. Point an `A` record such
as `app.example.com` to that address before starting the stack. In your AWS
security group or Contabo firewall, allow inbound TCP `80` and `443`; restrict
SSH (`22`) to your own IP address.

Install Docker Engine and the Compose plugin using Docker's current official
instructions for your server OS, then verify:

```sh
docker --version
docker compose version
```

## 2. Upload the project and configure secrets

```sh
git clone YOUR_REPOSITORY_URL ai-trader-copilot
cd ai-trader-copilot
cp .env.production.example .env
chmod 600 .env
```

Edit `.env` and replace every `replace-with-...` value. Set `DOMAIN`,
`PUBLIC_BASE_URL`, `TRUSTED_HOSTS`, and `OPENROUTER_HTTP_REFERER` to your real
domain. Keep `AUTH_JWT_SECRET` unchanged once customers have signed in.

Use a URL-safe PostgreSQL password (for example `openssl rand -hex 32`), since
Compose builds the internal database URL from that value.

## 3. Start and verify

```sh
docker compose config
docker compose up -d --build
docker compose ps
curl -fsS https://app.example.com/health
curl -fsS https://app.example.com/ready
```

The first start creates the PostgreSQL volume, runs Alembic migrations, and
asks Caddy to obtain the TLS certificate. Do not expose port `5432`.

## Performance and scale

The default Docker command uses one web process, which is the best starting
point for a low-RAM server. The application now shares short-lived market
snapshots, limits simultaneous full intelligence fetches, and keeps a bounded
in-memory cache; its default values are suitable for a 1–2 GB host.

For a larger server, scale web processes only after observing CPU and memory.
Keep `BACKGROUND_JOBS_ENABLED=true` in exactly one process. Set it to `false`
for every additional web worker/replica, otherwise each process would run the
same scanner, lifecycle monitor, and outcome tracker. Start with the following
settings in `.env`:

```text
MARKET_SNAPSHOT_CACHE_SECONDS=8
MARKET_SNAPSHOT_CACHE_MAX_ENTRIES=96
MARKET_INTELLIGENCE_MAX_CONCURRENCY=8
ANALYSIS_STREAM_MAX_PAIRS=32
ANALYSIS_STREAM_IDLE_SECONDS=30
ANALYSIS_COMPUTE_MAX_CONCURRENCY=4
ANALYSIS_COMPUTE_WAIT_SECONDS=20
BACKGROUND_JOBS_ENABLED=true
BACKGROUND_IDLE_CHECK_SECONDS=60
```

If market-data providers begin throttling requests, lower
`MARKET_INTELLIGENCE_MAX_CONCURRENCY` to `4`; if the host has ample capacity,
raise it cautiously, never above `32`.

Useful operations:

```sh
docker compose logs -f app
docker compose logs -f caddy
docker compose pull
docker compose up -d --build
```

## 4. Configure NOWPayments

Set the IPN secret in `.env`, then use this callback URL in NOWPayments:

```text
https://app.example.com/billing/webhooks/nowpayments
```

The application creates the callback URL in its selected-asset payment requests.
A signed webhook triggers an authoritative server-side provider lookup; the
authenticated payment-status recovery path performs the same lookup. Only a
provider-confirmed status activates access, never browser state or a return URL.

For testing, deploy a separate staging domain with sandbox credentials; do not
point NOWPayments sandbox callbacks at `localhost`.

## 5. Backups and upgrades

Make the scripts executable once:

```sh
chmod +x deploy/backup-postgres.sh deploy/restore-postgres.sh
```

Create a backup before every upgrade and copy it off the VPS:

```sh
./deploy/backup-postgres.sh
```

Restore deliberately (it replaces the database):

```sh
./deploy/restore-postgres.sh backups/ai-trader-YYYYMMDDTHHMMSSZ.sql.gz
```

For updates:

```sh
git pull
./deploy/backup-postgres.sh
docker compose up -d --build
docker compose ps
```

Never run `docker compose down -v` in production: the `-v` removes the
PostgreSQL and TLS certificate volumes.
