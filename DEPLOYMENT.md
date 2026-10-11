# ShopEase deployment

ShopEase is deployed with Docker Compose only. The production architecture is Caddy -> Django/Gunicorn -> PostgreSQL, with named volumes for database, media, and Caddy certificate state.

Follow [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) for the required untracked environment file, DNS/HTTPS configuration, data-preserving deployment commands, and verification checklist.
