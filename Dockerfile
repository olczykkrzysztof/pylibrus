# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# supercronic: a cron replacement built for containers. Unlike vixie-cron/busybox-cron
# it runs in the foreground, streams job stdout/stderr straight to the container's own
# stdout/stderr (so `docker logs` just works), and inherits the container's environment
# instead of needing it re-exported into the crontab.
ARG SUPERCRONIC_VERSION=v0.2.33

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) SC_ARCH=amd64 ;; \
      arm64) SC_ARCH=arm64 ;; \
      *) echo "Unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    BIN="supercronic-linux-${SC_ARCH}"; \
    BASE_URL="https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}"; \
    curl -fsSL -o /usr/local/bin/supercronic "${BASE_URL}/${BIN}"; \
    curl -fsSL -o /tmp/supercronic.sha1sum "${BASE_URL}/${BIN}.sha1sum"; \
    echo "$(cut -d' ' -f1 /tmp/supercronic.sha1sum)  /usr/local/bin/supercronic" | sha1sum -c -; \
    chmod +x /usr/local/bin/supercronic; \
    rm -f /tmp/supercronic.sha1sum

WORKDIR /app

# Install dependencies first so this layer is cached across source-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ src/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# CRON_SCHEDULE: standard 5-field crontab expression, how often pylibrus runs.
# CONFIG_FILE:   path to pylibrus.ini; mount it (read-only is fine) to use it.
#                If nothing is mounted here, pylibrus falls back to LIBRUS_* env vars
#                (single-user mode) automatically - no extra wiring needed.
# DATA_DIR:      where cookies and the per-user SQLite DB are kept; mount a volume
#                here so state survives container restarts/recreates.
ENV CRON_SCHEDULE="*/5 * * * *" \
    CONFIG_FILE=/config/pylibrus.ini \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data /config
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
