# =============================================================================
# Makefile - 本地开发一键入口
#
# 所有命令都通过 `uv run` 执行,确保使用项目锁定的环境。
# CI 工作流中调用同样的命令,保证本地与 CI 一致。
# =============================================================================
.PHONY: help install sync lint format typecheck test test-unit test-integration \
        db-up db-down db-logs migrate migrate-new run serve \
        web-install web-dev web-build web-lint \
        opencode-serve opencode-web mcp-serve \
        opencode-agent-image opencode-agent-smoke \
        worker dev clean

PYTHON ?= python3.12
UV ?= uv

# Windows 上 Node 可能通过安装器安装但未加入 PATH;允许命令行用 NPM=... 覆盖。
ifeq ($(OS),Windows_NT)
WIN_NODE_BIN ?= $(subst \,/,$(USERPROFILE))/AppData/Local/Programs/nodejs
NPM ?= $(WIN_NODE_BIN)/npm.cmd
else
NPM ?= npm
endif

help: ## 显示所有可用目标
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: sync ## 安装/同步依赖(等同于 sync)

sync: ## 同步全部 workspace 依赖(包含 dev)
	$(UV) sync --all-packages

lint: ## 静态检查(ruff check)
	$(UV) run ruff check .

format: ## 自动格式化(ruff format + ruff check --fix)
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## 类型检查(mypy,严格模式)
	$(UV) run mypy .

test: ## 运行全部测试(默认跳过 integration,需要 PostgreSQL)
	$(UV) run pytest -m "not integration"

test-unit: ## 仅运行单元测试
	$(UV) run pytest tests/unit -m "not slow"

test-integration: ## 仅运行集成测试(需要 PostgreSQL,见 make db-up)
	$(UV) run pytest tests/integration -m "integration"

db-up: ## 启动本地 PostgreSQL(docker compose)
	docker compose up -d postgres
	@echo "等待 postgres 就绪..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		docker compose exec -T postgres pg_isready -U finboard >/dev/null 2>&1 && break; \
		sleep 1; \
	done
	@echo "PostgreSQL 已就绪: docker compose logs -f postgres"

db-down: ## 停止本地 PostgreSQL
	docker compose down

db-logs: ## 查看 PostgreSQL 日志
	docker compose logs -f postgres

migrate: ## 应用数据库迁移到最新版本
	$(UV) run alembic upgrade head

migrate-new: ## 生成新的迁移文件: make migrate-new name=add_xxx
	@if [ -z "$(name)" ]; then echo "Usage: make migrate-new name=<migration_name>"; exit 1; fi
	$(UV) run alembic revision --autogenerate -m "$(name)"

run: ## 启动交易核心进程(mock broker 默认)
	$(UV) run finboard run

serve: ## 启动 FastAPI 后端(端口 8000)
	$(UV) run finboard serve

reconcile: ## 执行一次本地 ↔ 券商核对
	$(UV) run finboard reconcile

# --------------------------------------------------------------------------- 前端
WEB_DIR ?= web
# dev 端口(多 worktree 并行开发,#289):主目录用默认值;
# worktree N 用 `make dev API_PORT=$((8000+N)) WEB_PORT=$((5172+N))` 覆盖。
API_PORT ?= 8000
WEB_PORT ?= 5173

web-install: ## 安装前端依赖(npm install)
	cd $(WEB_DIR) && $(NPM) install

web-dev: ## 启动前端 Vite dev server(端口 5173,代理 /api + /ws 到 :8000)
	cd $(WEB_DIR) && $(NPM) run dev

web-build: ## 构建前端生产包到 web/dist
	cd $(WEB_DIR) && $(NPM) run build

web-lint: ## 前端 ESLint
	cd $(WEB_DIR) && $(NPM) run lint

# --------------------------------------------------------------------------- OpenCode 研究运行时(#108/#109/#118)
OPENCODE_PORT ?= 4097
OPENCODE_SERVE_PORT ?= 4096
# iframe 跨源嵌入必须允许 FinBoard 前端源
OPENCODE_CORS ?= http://localhost:5173
# HTTP 传输强制 Bearer 鉴权(空 token 拒绝启动);用 make mcp-serve-http MCP_AUTH_TOKEN=xxx 覆盖
MCP_AUTH_TOKEN ?= change-me

opencode-serve: ## 启动 opencode serve(headless HTTP API,端口 $(OPENCODE_SERVE_PORT))
	opencode serve --port $(OPENCODE_SERVE_PORT) --hostname 127.0.0.1 --cors $(OPENCODE_CORS)

opencode-web: ## 启动 opencode web(带 Web UI,端口 $(OPENCODE_PORT));FinBoard 托管时无需手动启动
	opencode web --port $(OPENCODE_PORT) --hostname 127.0.0.1 --cors $(OPENCODE_CORS)

mcp-serve: ## 启动 finboard-mcp(stdio 传输,供 OpenCode 子进程接入)
	FINBOARD_MCP_ENABLED=true $(UV) run python -m finboard_mcp

mcp-serve-http: ## 启动 finboard-mcp(HTTP 传输,Docker 隔离前置;容器内 opencode 通过 host.docker.internal:8765 接入;须设 MCP_AUTH_TOKEN)
	FINBOARD_MCP_ENABLED=true FINBOARD_MCP_TRANSPORT=streamable-http FINBOARD_MCP_HOST=0.0.0.0 FINBOARD_MCP_PORT=8765 FINBOARD_MCP_AUTH_TOKEN=$(MCP_AUTH_TOKEN) $(UV) run python -m finboard_mcp

# 衍生镜像 tag:与 Dockerfile ARG BASE_IMAGE 的 opencode 版本同步 bump(#313)。
OPENCODE_AGENT_IMAGE ?= finboard-opencode-agent:1.18.15

opencode-agent-image: ## 构建 OpenCode 容器衍生镜像($(OPENCODE_AGENT_IMAGE);官方镜像 + jq/python3,见 docs/opencode-agent-image.md)
	docker build -f docker/opencode-agent/Dockerfile \
		-t $(OPENCODE_AGENT_IMAGE) docker/opencode-agent/

opencode-agent-smoke: ## 衍生镜像 smoke:jq/python3 可用 + ENTRYPOINT 完好
	docker run --rm $(OPENCODE_AGENT_IMAGE) --version
	docker run --rm --entrypoint /bin/sh $(OPENCODE_AGENT_IMAGE) -c 'jq --version && python3 --version'

# --------------------------------------------------------------------------- 后台任务队列
worker: ## 启动后台任务 worker(消费研究/数据 job 队列;make dev 已默认附带)
	$(UV) run finboard worker run

# --------------------------------------------------------------------------- 一键开发
dev: ## 一键启动开发环境(后端 :$(API_PORT) + 前端 :$(WEB_PORT) + 后台 worker,Ctrl-C 同时退出)
	@echo "\033[36m启动后端(FastAPI :$(API_PORT)) + 前端(Vite :$(WEB_PORT)) + 后台 worker...\033[0m"
	@echo "\033[33m本机数据库不可达时会自动唤醒 WSL PostgreSQL;请确保已执行 make migrate\033[0m"
	@echo "\033[33m研究/数据 job 由随 dev 启动的 worker 消费;独立部署请用 make worker\033[0m"
	FINBOARD_WEB_PORT=$(WEB_PORT) FINBOARD_WEB_API_PORT=$(API_PORT) $(UV) run finboard dev --port $(API_PORT) --web-dir $(WEB_DIR)

clean: ## 清理缓存与构建产物
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +
