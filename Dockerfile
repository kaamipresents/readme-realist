# syntax=docker/dockerfile:1

# --- build -----------------------------------------------------------------
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Dependency metadata only, so the wheel layer caches across source edits.
COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# --- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 realist

COPY --from=build /opt/venv /opt/venv

WORKDIR /srv
COPY app ./app

USER realist

EXPOSE 8000

# The container has no shell tooling by design; use Python for the probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# Launch through `app.main`'s own entrypoint: it calls `uvicorn.run(...,
# log_config=None, proxy_headers=True)`, so the app's structured-JSON logging
# owns every handler and the client IP/scheme survive the platform load
# balancer. The old `uvicorn --log-config /dev/null` form now aborts at boot —
# modern uvicorn feeds the path to `logging.config.fileConfig`, which rejects an
# empty file. Honours `$PORT` for Cloud Run / Railway.
CMD ["python", "-m", "app.main"]
