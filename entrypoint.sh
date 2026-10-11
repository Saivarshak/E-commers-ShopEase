#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    python manage.py migrate --noinput
fi

if [ "${BOOTSTRAP_CATALOG:-true}" = "true" ]; then
    python manage.py bootstrap_catalog
fi

if [ "${COLLECT_STATIC:-true}" = "true" ]; then
    python manage.py collectstatic --noinput
fi

if [ "${CREATE_SUPERUSER:-false}" = "true" ]; then
    python manage.py ensure_superuser
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec gunicorn shopease.wsgi:application \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers "${WEB_CONCURRENCY:-2}" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
    --keep-alive "${GUNICORN_KEEP_ALIVE:-5}" \
    --no-control-socket \
    --access-logfile - \
    --error-logfile -
