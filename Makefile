# =============================================================================
# Makefile - 本地开发一键入口
#
# 所有命令都通过 `uv run` 执行,确保使用项目锁定的环境。
# CI 工作流中调用同样的命令,保证本地与 CI 一致。
# =============================================================================
.PHONY: help install sync lint format typecheck test test-unit test-integration \
        db-up db-down db-logs migrate migrate-new run serve \
        web-install web-dev web-build web-lint \
        mcp-serve mcp-serve-http mcp-serve-http-remote \
        mcp-client-create mcp-client-list mcp-client-revoke \
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

# --------------------------------------------------------------------------- MCP 研究工具服务(#108/#517)
# HTTP 传输必须有服务端凭据(#517):推荐用 `make mcp-client-create` 签发
# 按客户端凭据;旧共享 token 仅在兼容期通过 MCP_AUTH_TOKEN 显式传入(默认空)。
MCP_AUTH_TOKEN ?=
MCP_CLIENT_NAME ?= codex
MCP_CLIENT_PRESET ?= readonly

mcp-client-create: ## 签发 MCP 客户端凭据(NAME=$(MCP_CLIENT_NAME) PRESET=$(MCP_CLIENT_PRESET);明文 token 只打印一次)
	$(UV) run finboard mcp client create --name $(MCP_CLIENT_NAME) --preset $(MCP_CLIENT_PRESET)

mcp-client-list: ## 列出 MCP 客户端(不显示 secret / 摘要)
	$(UV) run finboard mcp client list

mcp-client-revoke: ## 吊销 MCP 客户端(需 CLIENT_ID=MC-xxx)
	$(UV) run finboard mcp client revoke $(CLIENT_ID)

mcp-serve: ## 启动 finboard-mcp(stdio 传输,本机子进程接入;默认作用域 research.read)
	FINBOARD_MCP_ENABLED=true $(UV) run python -m finboard_mcp

mcp-serve-http: ## 启动 finboard-mcp(本机 loopback HTTP;凭据来自 mcp_clients,或兼容期 MCP_AUTH_TOKEN)
	FINBOARD_MCP_ENABLED=true FINBOARD_MCP_TRANSPORT=streamable-http FINBOARD_MCP_HOST=127.0.0.1 FINBOARD_MCP_PORT=8765 FINBOARD_MCP_AUTH_TOKEN="$(MCP_AUTH_TOKEN)" $(UV) run python -m finboard_mcp

mcp-serve-http-remote: ## 非 loopback HTTP(容器/远程;须 TLS 反向代理 + 凭据;显式 ALLOW_REMOTE)
	FINBOARD_MCP_ENABLED=true FINBOARD_MCP_TRANSPORT=streamable-http FINBOARD_MCP_HOST=0.0.0.0 FINBOARD_MCP_ALLOW_REMOTE=true FINBOARD_MCP_PORT=8765 FINBOARD_MCP_AUTH_TOKEN="$(MCP_AUTH_TOKEN)" $(UV) run python -m finboard_mcp

# --------------------------------------------------------------------------- 后台任务队列
worker: ## 启动后台任务 worker(消费研究/数据 job 队列;make dev 已默认附带)
	$(UV) run finboard worker run

# --------------------------------------------------------------------------- 一键开发
# DEV_ARGS 透传给 finboard dev,例如 `make dev DEV_ARGS=--no-mcp`。
DEV_ARGS ?=
dev: ## 一键启动开发环境(API :$(API_PORT) + 前端 :$(WEB_PORT) + worker + MCP,Ctrl-C 同时退出)
	@echo "\033[36m启动后端(FastAPI :$(API_PORT)) + 前端(Vite :$(WEB_PORT)) + 后台 worker + MCP 研究服务...\033[0m"
	@echo "\033[33m本机数据库不可达时会自动唤醒 WSL PostgreSQL;请确保已执行 make migrate\033[0m"
	@echo "\033[33mMCP 仅在 mcp_transport 非 stdio(streamable-http/sse)且 mcp_enabled=true 时托管,默认 8765(受 FINBOARD_MCP_PORT 控制);开关用 make dev DEV_ARGS=--no-mcp / --no-worker\033[0m"
	FINBOARD_WEB_PORT=$(WEB_PORT) FINBOARD_WEB_API_PORT=$(API_PORT) $(UV) run finboard dev --port $(API_PORT) --web-dir $(WEB_DIR) $(DEV_ARGS)

clean: ## 清理缓存与构建产物
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +
