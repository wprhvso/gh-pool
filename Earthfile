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
    COPY protocol/pyproject.toml protocol/pyproject.toml
    COPY client/pyproject.toml client/pyproject.toml
    COPY server/pyproject.toml server/pyproject.toml
    COPY runner/pyproject.toml runner/pyproject.toml
    RUN mkdir -p protocol/gh_chrome_protocol client/gh_chrome server/gh_chrome_server runner/gh_chrome_runner && touch protocol/gh_chrome_protocol/__init__.py client/gh_chrome/__init__.py server/gh_chrome_server/__init__.py runner/gh_chrome_runner/__init__.py
    RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --package gh-chrome-server --no-install-workspace
    SAVE ARTIFACT /app/.venv

build:
    FROM +deps
    COPY protocol protocol
    COPY server server
    RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --package gh-chrome-server
    SAVE ARTIFACT /app/.venv venv
    SAVE ARTIFACT /app/protocol protocol
    SAVE ARTIFACT /app/server server

lint:
    FROM +deps
    COPY . .
    RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --all-packages
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
    COPY +build/protocol /app/protocol
    COPY +build/server /app/server
    USER gh-chrome
    VOLUME /var/lib/gh-chrome
    EXPOSE 8000
    ENTRYPOINT ["gh-chrome-server"]
    SAVE IMAGE --push $IMAGE:$VERSION $IMAGE:latest

multi:
    ARG VERSION=dev
    BUILD --platform=linux/amd64 --platform=linux/arm64 +docker --VERSION=$VERSION
