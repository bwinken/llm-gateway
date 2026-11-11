# LLM Gateway image. Build from the repository root (the build context):
#   docker build -f deploy/migration/Dockerfile -t llm-gateway:<version> .
# or: bash deploy/migration/compose.sh build gateway
# What stays out of the build context is listed in Dockerfile.dockerignore, next
# to this file. Run the image with compose.sh, which supplies the mounts and
# settings below; see deploy/migration/README.md.
#
# What lives outside the image (never baked in):
#   /app/config      config.toml (CONFIG_DIR). Mount the DIRECTORY: the admin
#                    page saves it with a temp file + rename in that directory.
#   /app/keys        AuthCenter public key (read-only; mount the directory so a
#                    replaced key file is seen)
#   /app/logs        per-worker log files (LOG_DIR); stdout carries the same lines
#   .env             secrets and settings, passed as environment variables
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

FROM ${UV_IMAGE} AS uv

# ── Dependencies, exactly as uv.lock pins them ──
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# ── Runtime: no uv, no build tools ──
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PATH=/app/.venv/bin:$PATH \
    CONFIG_DIR=/app/config \
    METRICS_DIR=/tmp/llm-gateway-metrics
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini config.toml.example pyproject.toml ./
COPY alembic/ alembic/
COPY app/ app/
COPY scripts/ scripts/
COPY deploy/migration/wait_for_db.py scripts/wait_for_db.py
COPY setup/ setup/
# Code stays root-owned and read-only. The writable directories accept any
# uid, because compose runs the container as the host user that owns the
# mounted config / logs (GATEWAY_UID); they are normally mounts anyway.
RUN python -m compileall -q app scripts \
    && mkdir -p config keys logs \
    && chmod 1777 config logs \
    && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin gateway
USER 1000

EXPOSE 8050
# 127.0.0.1 by default: with host networking (the compose default) only the
# host's nginx reaches it. Set GATEWAY_HOST=0.0.0.0 only on a bridge network.
CMD ["sh", "-c", "exec fastapi run app/main.py --host \"${GATEWAY_HOST:-127.0.0.1}\" --port \"${GATEWAY_PORT:-8050}\" --workers \"${GATEWAY_WORKERS:-2}\""]
