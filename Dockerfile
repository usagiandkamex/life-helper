# syntax=docker/dockerfile:1

# ---- frontend build ------------------------------------------------------------------------------------
FROM node:24-slim AS frontend
WORKDIR /src
COPY app/frontend/package.json app/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY app/frontend/ ./
RUN npm run build

# ---- runtime -------------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/home/app \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    LH_DATA_DIR=/data \
    LH_STATIC_DIR=/app/static

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv
RUN useradd --create-home --home-dir /home/app --uid 10001 app \
    && mkdir -p /app/backend /data \
    && chown -R app:app /app /data

WORKDIR /app/backend
COPY --chown=app:app app/backend/pyproject.toml ./
USER app
# Dependencies first (without the app) so source changes do not reinstall them or Chromium.
RUN uv sync --no-dev --no-install-project \
    # Pre-download the Copilot runtime so containers start without network downloads.
    && python -m copilot download-runtime

USER root
# Headless Chromium for the browser_* tools with its system libraries, plus a Japanese font for screenshots.
RUN playwright install --with-deps --only-shell chromium \
    && apt-get install -y --no-install-recommends fonts-ipafont-gothic \
    && rm -rf /var/lib/apt/lists/*

USER app
COPY --chown=app:app app/backend/src ./src
RUN uv sync --no-dev --no-editable

COPY --from=frontend --chown=app:app /src/dist /app/static

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
# The web app. The scheduled ACA job uses the same image with the command: life-helper-job
# Open connections (chat streams) get 10 seconds when the container is stopped, so the shutdown that records automation
# runs in progress as interrupted runs within the platform's grace period.
CMD ["uvicorn", "life_helper.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "10"]
