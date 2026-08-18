FROM python:3.13-slim

RUN useradd --create-home --uid 1000 runners

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pool_runners/ pool_runners/

RUN pip install --no-cache-dir --disable-pip-version-check .

USER runners
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["pool-runners"]
