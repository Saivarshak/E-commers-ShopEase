# ShopEase bandwidth monitoring

Use `scripts/bandwidth_audit.py` to measure local Django response sizes without contacting production or the production PostgreSQL database. The script copies the local SQLite database into a temporary isolated directory, requests representative pages and optimized images with Django’s test client, aggregates bytes by path, and can fail when configured budgets are exceeded.

## Basic audit

From the repository root, run:

```powershell
python scripts/bandwidth_audit.py --output bandwidth-baseline.json
```

The report includes HTTP status, response bytes, content type, cache headers, total megabytes, and per-route totals. The generated JSON report is local measurement output and should not be committed unless the team intentionally wants a historical baseline.

## Mock traffic test

Repeat the representative routes to simulate repeated page visits:

```powershell
python scripts/bandwidth_audit.py `
  --repeat 10 `
  --fail-if-total-mb 1 `
  --fail-if-route-kb 150 `
  --output bandwidth-baseline.json
```

Use additional `--path` arguments when testing a particular flow:

```powershell
python scripts/bandwidth_audit.py `
  --path / `
  --path /mens/ `
  --path /womens/ `
  --path /kids/ `
  --path /static/images/shirt.webp `
  --repeat 10
```

The test is intentionally local. It is not a production load test and must not be pointed at a public production URL. Do not use it to generate high-volume traffic against a public service.

## Current baseline

The initial local run after image optimization produced 18 successful responses for six paths repeated three times. The total response payload was approximately 0.307 MB. The largest individual response in that set was `static/images/shirt.webp` at approximately 29 KB per request. This baseline measures only the selected paths and does not prove the historical cause of prior hosting bandwidth usage.

## Pre-push checks

Run these checks before committing bandwidth-related changes:

```powershell
python -m py_compile scripts/bandwidth_audit.py
python manage.py check
python scripts/bandwidth_audit.py --repeat 3 --fail-if-total-mb 1 --fail-if-route-kb 150
```

If the local `.env` contains malformed lines or production `DATABASE_URL`, the script may print dotenv warnings. It overrides the database connection for the audit, but it does not edit `.env` and does not contact production.

## Interpretation

Use the audit to detect regressions in bytes per page visit. A smaller repository or database does not automatically mean lower hosting bandwidth. Compare the same route set before and after each optimization, and inspect images, HTML, JSON, static assets, repeated requests, redirects, and bots separately. Confirm live behavior with the hosting provider's request logs or metrics after deployment.
