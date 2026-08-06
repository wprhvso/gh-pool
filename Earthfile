VERSION 0.8

PROJECT wprhvso/gh-browser

ARG --global IMAGE=ghcr.io/wprhvso/gh-chrome
ARG --global PYTHON=3.13

deps:
    FROM ghcr.io/astral-sh/uv:python$PYTHON-bookworm-slim
    ENV UV_COMPILE_BYTECODE=1
    ENV UV_LINK_MODE=copy
    ENV UV_PYTHON_DOWNLOADS=never
    WORKDIR /app
    COPY pyproject.toml uv.lock ./
    RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

build:
    FROM +deps
    COPY src src
    RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable
    SAVE ARTIFACT /app/.venv venv

lint:
    FROM +deps
    COPY . .
    RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
    RUN uv run ruff check .
    RUN uv run ruff format --check .

docker:
    FROM python:$PYTHON-slim-bookworm
    ARG TARGETARCH
    ARG VERSION=dev
    ENV PATH=/app/.venv/bin:$PATH
    ENV PYTHONUNBUFFERED=1
    ENV GH_CHROME_HOST=0.0.0.0
    ENV GH_CHROME_STORAGE=/var/lib/gh-chrome
    WORKDIR /app
    RUN useradd --system --create-home --uid 10001 gh-chrome && mkdir -p /var/lib/gh-chrome && chown gh-chrome:gh-chrome /var/lib/gh-chrome
    COPY +build/venv /app/.venv
    USER gh-chrome
    VOLUME /var/lib/gh-chrome
    EXPOSE 8000
    ENTRYPOINT ["gh-chrome-server"]
    SAVE IMAGE --push $IMAGE:$VERSION $IMAGE:latest

multi:
    ARG VERSION=dev
    BUILD --platform=linux/amd64 --platform=linux/arm64 +docker --VERSION=$VERSION
