FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 10001 appuser \
    && pip install --no-cache-dir "uv>=0.5"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY installer ./installer
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=web /web/dist ./web/dist
# The data directory is created owned by the runtime user so a fresh named volume is writable.
RUN mkdir -p /app/data && chown appuser:appuser /app/data

USER appuser
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEB_PORT', '8080'), timeout=4)"]
CMD ["/app/.venv/bin/python", "-m", "app"]
