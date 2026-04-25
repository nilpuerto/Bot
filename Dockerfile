# syntax=docker/dockerfile:1.7
# Multi-stage Docker image for the Prym Signals bot.
#
# Build:   docker build -t prym-bot .
# Run:     docker run --env-file .env --restart unless-stopped prym-bot
# Or:      docker compose up -d    (reads docker-compose.yml + .env)

# ---- builder: install deps into a throwaway layer ---------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build tools needed by some transitive wheels (cytoolz, pycryptodome, ...).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ---- runtime: lean final image ---------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=prod \
    TZ=UTC

# Non-root user for safety.
RUN groupadd --system prym \
 && useradd --system --gid prym --home /app --shell /usr/sbin/nologin prym

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=prym:prym . /app

USER prym

# Periodic liveness probe — the orchestrator writes a timestamp to this file
# on every housekeeping tick; if it ever stalls, the container is restarted.
HEALTHCHECK --interval=2m --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import asyncio,sys; from app.integrations.rss_client import RSSClient; \
                   asyncio.run(RSSClient().__aenter__()) or sys.exit(0)" || exit 1

# Default command — the long-running orchestrator.
CMD ["python", "-m", "app.main"]
