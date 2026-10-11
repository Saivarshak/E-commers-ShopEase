# ShopEase Docker production runbook

ShopEase is deployed as a single Docker Compose stack:

```text
Internet -> Caddy (:80/:443) -> Django/Gunicorn (internal :8080) -> PostgreSQL (internal :5432)
```

The `db` service has no published port. Caddy owns the public HTTP/HTTPS ports, terminates TLS, and proxies requests to Gunicorn. Django serves collected static files through WhiteNoise. Product and category uploads are stored in PostgreSQL through `UploadedImage`; the `media_data` named volume preserves the legacy filesystem upload fallback.

The stack has four persistent named volumes:

- `postgres_data` — PostgreSQL data
- `media_data` — legacy media uploads
- `caddy_data` — certificates and ACME state
- `caddy_config` — Caddy runtime configuration

Do not run `docker compose down -v` on a deployed stack. Plain `docker compose down` preserves all four volumes.

## Production environment

Copy `.env.example` to an untracked, host-only file, for example `/etc/shopease/production.env` (or `.env.production` for a local smoke test). Replace every example domain and secret before deployment. Do not use the workspace development `.env` as a Docker deployment file.

Required values include:

```text
CADDY_SITE=shop.example.com
APP_BASE_URL=https://shop.example.com
PAYMENT_CALLBACK_BASE_URL=https://shop.example.com
ALLOWED_HOSTS=shop.example.com,www.shop.example.com
CSRF_TRUSTED_ORIGINS=https://shop.example.com,https://www.shop.example.com
SECRET_KEY=<unique Django secret>
POSTGRES_DB=shopease
POSTGRES_USER=shopease
POSTGRES_PASSWORD=<unique PostgreSQL password>
```

The Compose file deliberately forces Django to use the internal `db` service (`DB_HOST=db` and `POSTGRES_HOST=db`) and clears `DATABASE_URL`. This prevents a loopback or stale external URL from bypassing the persistent Compose PostgreSQL database.

## Deploy

On the Docker host, point DNS for `CADDY_SITE` at the host before starting the stack, and ensure inbound TCP 80 and 443 are open. Caddy will obtain and renew the HTTPS certificate automatically.

PowerShell:

```powershell
$env:ENV_FILE = 'C:\secure\shopease.production.env'
docker compose --env-file $env:ENV_FILE config --quiet
docker compose --env-file $env:ENV_FILE build --progress=plain
docker compose --env-file $env:ENV_FILE up -d
docker compose --env-file $env:ENV_FILE ps
docker compose --env-file $env:ENV_FILE logs --tail=200
```

Linux/macOS:

```sh
export ENV_FILE=/etc/shopease/production.env
docker compose --env-file "$ENV_FILE" config --quiet
docker compose --env-file "$ENV_FILE" build --progress=plain
docker compose --env-file "$ENV_FILE" up -d
docker compose --env-file "$ENV_FILE" ps
docker compose --env-file "$ENV_FILE" logs --tail=200
```

`config --quiet` rejects missing domain, secret, or PostgreSQL initialization values before any container starts. The web entrypoint runs `migrate --noinput` and `collectstatic --noinput` before Gunicorn starts. The same migration command is safe to run explicitly after startup:

```powershell
docker compose --env-file $env:ENV_FILE exec web python manage.py migrate --noinput
docker compose --env-file $env:ENV_FILE exec web python manage.py check --deploy
```

## Existing data

An existing `postgres_data` volume is authoritative and is never replaced by the Compose configuration. Do not run a fixture load, initialization script, or `down -v` against it.

For a first PostgreSQL deployment sourced from the local SQLite database, take a verified backup first and import a reviewed, private fixture only into a fresh empty PostgreSQL volume. Keep exports outside Git and outside the Docker build context. The import must be a deliberate one-time migration; it must not be added to the container entrypoint because doing so could overwrite administrator changes on later restarts.

Before making a one-time import, capture a rollback backup:

```powershell
docker compose --env-file $env:ENV_FILE exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > .\shopease-before-import.sql
```

Verify product, category, order, user, and uploaded-image counts before and after importing. Never copy an unreviewed fixture into a live database.

## Verification

After all three containers report healthy, test through the public HTTPS domain, not the internal web port:

```powershell
python scripts/verify_production_https.py https://shop.example.com
```

The script covers health, HTTP-to-HTTPS redirect, home, category/search/product pages, API routes, static assets, images, registration/login/logout, cart, wishlist, checkout, orders, payment-endpoint behavior, Django admin reachability, deep URL refreshes, and security headers. Configure real Razorpay live credentials and perform a separately authorized low-value payment before calling payment processing live.

Useful operational checks:

```powershell
docker compose --env-file $env:ENV_FILE ps
docker compose --env-file $env:ENV_FILE logs web db caddy --tail=200
docker compose --env-file $env:ENV_FILE exec web python manage.py check
docker compose --env-file $env:ENV_FILE exec web python manage.py check --deploy
```

## Security notes

- `DEBUG` is forced off inside the web service.
- Secrets remain in the selected ignored environment file, never in Git.
- PostgreSQL is reachable only over the Compose network.
- Caddy handles HTTPS; Django receives the forwarded HTTPS headers and uses secure session/CSRF cookies.
- The setup has no provider-specific service, manifest, URL, webhook, environment variable, or runtime dependency.

## Live deployment (SnapDeploy + Supabase)

- Live URL: https://shopease.containers.snapdeploy.app (HTTPS certificate issued by the platform on first deploy)
- Backend/API: same origin under `/api/` (e.g. `/api/products/`)
- App host: SnapDeploy free tier — the GitHub repository is built from the
  `Dockerfile` on branch `codex/production-deployment`; environment variables
  live in the SnapDeploy dashboard, never in Git.
- Database: Supabase PostgreSQL 17 (pooled IPv4 endpoint, `sslmode=require`),
  seeded from `backups/` (which is git-ignored).
- The container sleeps after ~15 idle minutes; a browser visit wakes it in
  about a minute (the API `wake` endpoint does the same for scripts).
- SnapDeploy's edge terminates TLS but does not forward `X-Forwarded-Proto`,
  so `SECURE_SSL_REDIRECT` stays off (the platform health-checks over plain
  HTTP with `Host: localhost`) and HSTS is emitted by
  `store.middleware.ProductionSecurityHeadersMiddleware` instead.
- Deploy limit: 5 deploys per rolling 12 hours on the free tier.
