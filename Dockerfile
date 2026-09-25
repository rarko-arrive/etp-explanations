# syntax=docker/dockerfile:1

# ETP explainer — production image for Azure ML Custom Applications.
# Build (requires GitHub SSH for private arriveds dep):
#   DOCKER_BUILDKIT=1 docker build --ssh default -t etp-explainer:local .
# Run locally:
#   docker compose up

ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

WORKDIR /src

# Private git dep (arriveds) — forward host agent: docker build --ssh default
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client git \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p -m 0700 /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY app ./app
COPY scripts ./scripts

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN --mount=type=ssh \
    uv sync --frozen --no-dev --group serve --no-editable

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="etp-explainer" \
      org.opencontainers.image.description="ETP shipment lifecycle explainer (FastAPI)"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    # Container defaults — override via Azure ML env / bind mounts
    DQT_DATA_DIR=/data \
    EXPLAIN_CACHE_DIR=/cache \
    EXPLAIN_HOST=0.0.0.0 \
    EXPLAIN_PORT=8765 \
    EXPLAIN_BEHIND_PROXY=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        fonts-dejavu-core \
        libfreetype6 \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /cache /tmp/matplotlib \
    && chown -R app:app /app /cache /tmp/matplotlib

WORKDIR /app

COPY --from=builder --chown=app:app /src/.venv /app/.venv
COPY --chown=app:app pyproject.toml ./
COPY --chown=app:app SQL/etp-slider ./SQL/etp-slider
COPY --chown=app:app app ./app
COPY --chown=app:app scripts/explain_serve.py ./scripts/explain_serve.py
COPY --chown=app:app docker/entrypoint.sh /entrypoint.sh

RUN chmod 0755 /entrypoint.sh

USER app

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${EXPLAIN_PORT}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
