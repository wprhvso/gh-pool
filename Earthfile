VERSION 0.8

PROJECT wprhvso/gh-pool

ARG --global IMAGE=ghcr.io/wprhvso/gh-pool
ARG --global PYTHON=3.14

deps:
    FROM ghcr.io/astral-sh/uv:python$PYTHON-bookworm-slim
    ENV UV_COMPILE_BYTECODE=1
    ENV UV_LINK_MODE=copy
    ENV UV_PYTHON_DOWNLOADS=never
    WORKDIR /app
    COPY pyproject.toml uv.lock ./
    RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --extra server --extra relay --no-dev --no-install-project

build:
    FROM +deps
    COPY src src
    RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --extra server --extra relay --no-dev --no-editable
    SAVE ARTIFACT /app/.venv venv

docker:
    FROM python:$PYTHON-slim-bookworm
    ARG TARGETARCH
    ARG VERSION=dev
    # A prerelease is tagged like any other version and must not become what
    # anyone gets by asking for nothing in particular.
    ARG LATEST=false
    ENV PATH=/app/.venv/bin:$PATH
    ENV PYTHONUNBUFFERED=1
    ENV GH_POOL_HOST=0.0.0.0
    ENV GH_POOL_RELAY_HOST=0.0.0.0
    ENV GH_POOL_STORAGE=/var/lib/gh-chrome
    WORKDIR /app
    RUN useradd --system --create-home --uid 10001 gh-pool && mkdir -p /var/lib/gh-chrome && chown gh-pool:gh-pool /var/lib/gh-chrome
    COPY +build/venv /app/.venv
    USER gh-pool
    VOLUME /var/lib/gh-chrome
    EXPOSE 8000
    EXPOSE 8001
    # Один образ на оба процесса: relay поднимается тем же образом с
    # command: ["gh-pool-relay"], как и миграции с ["alembic", "upgrade", "head"].
    ENTRYPOINT ["gh-pool-server"]
    IF [ "$LATEST" = "true" ]
        SAVE IMAGE --push $IMAGE:$VERSION $IMAGE:latest
    ELSE
        SAVE IMAGE --push $IMAGE:$VERSION
    END

multi:
    ARG VERSION=dev
    ARG LATEST=false
    BUILD --platform=linux/amd64 --platform=linux/arm64 +docker \
        --VERSION=$VERSION --LATEST=$LATEST
