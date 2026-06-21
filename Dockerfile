FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update -yq && \
    apt-get install -yq --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-extras --all-groups

#------------------------------------------------
# Runtime
#------------------------------------------------
FROM python:3.14-slim-trixie AS runtime

ENV LNCRAWL_DATA_PATH=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

COPY pyproject.toml uv.lock ./
COPY lncrawl ./lncrawl
COPY sources ./sources

ENTRYPOINT ["/app/.venv/bin/python", "-m", "lncrawl"]
