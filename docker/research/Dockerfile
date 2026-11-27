FROM node:24-bookworm-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN VITE_FINBOARD_RESEARCH_EDITION=true npm run build

FROM python:3.12-slim-bookworm AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /bin/uv
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package findashboard --package finboard-api

FROM python:3.12-slim-bookworm
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FINBOARD_RESEARCH_WEB_ROOT=/opt/finboard/web FINBOARD_RESEARCH_SANDBOX_ENABLED=false \
    FINBOARD_RESEARCH_DOCS_DIR=/opt/finboard/docs/research
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 finboard \
    && install -d -o finboard -g finboard /state
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=web /src/web/dist /opt/finboard/web
COPY alembic.ini /opt/finboard/
COPY migrations/ /opt/finboard/migrations/
COPY docs/research/ /opt/finboard/docs/research/
COPY docker/research/entrypoint.py /opt/finboard/
WORKDIR /state
USER finboard
EXPOSE 8000
ENTRYPOINT ["python", "/opt/finboard/entrypoint.py"]
CMD ["gateway"]
