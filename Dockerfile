FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system django && adduser --system --ingroup django django

COPY requirements.txt requirements.production.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.production.txt

COPY --chown=django:django . /app/
RUN mkdir -p /app/media /app/staticfiles \
    && chown -R django:django /app/media /app/staticfiles

COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh

USER django
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
