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
```

The first start creates the PostgreSQL volume, runs Alembic migrations, and
asks Caddy to obtain the TLS certificate. Do not expose port `5432`.

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

The application creates the callback URL in its invoice requests and only
activates subscriptions after the signed webhook and a server-side provider
status check. A browser return URL never activates an account by itself.

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
