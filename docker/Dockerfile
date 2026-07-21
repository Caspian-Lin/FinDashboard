# 生产环境交易核心镜像。
# 仅作 P0 演示用,实盘部署前需补充 secrets 注入、非 root 用户、镜像签名等。
# QMT 的 xtquant 仅 Windows 提供,CTP SDK 视券商提供版本决定,默认镜像仅包含 mock broker。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock* alembic.ini ./
COPY packages ./packages
COPY migrations ./migrations

RUN uv sync --all-packages --no-dev --no-install-project

COPY . .

RUN useradd --create-home --uid 1000 finboard \
    && chown -R finboard:finboard /app
USER finboard

EXPOSE 8000

ENTRYPOINT ["uv", "run", "finboard"]
CMD ["run"]
