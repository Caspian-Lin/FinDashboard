# =============================================================================
# Makefile - 本地开发一键入口
#
# 所有命令都通过 `uv run` 执行,确保使用项目锁定的环境。
# CI 工作流中调用同样的命令,保证本地与 CI 一致。
# =============================================================================
.PHONY: help install sync lint format typecheck test test-unit test-integration \
        db-up db-down db-logs migrate migrate-new run serve \
        web-install web-dev web-build web-lint \
        dev clean

PYTHON ?= python3.12
UV ?= uv

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

serve: ## 启动 FastAPI 后端(--reload 热重载,端口 8000)
	$(UV) run finboard serve --reload

reconcile: ## 执行一次本地 ↔ 券商核对
	$(UV) run finboard reconcile

# --------------------------------------------------------------------------- 前端
WEB_DIR ?= web

web-install: ## 安装前端依赖(npm install)
	cd $(WEB_DIR) && npm install

web-dev: ## 启动前端 Vite dev server(端口 5173,代理 /api + /ws 到 :8000)
	cd $(WEB_DIR) && npm run dev

web-build: ## 构建前端生产包到 web/dist
	cd $(WEB_DIR) && npm run build

web-lint: ## 前端 ESLint
	cd $(WEB_DIR) && npm run lint

# --------------------------------------------------------------------------- 一键开发
dev: ## 一键启动前后端开发服务器(后端 :8000 + 前端 :5173,Ctrl-C 同时退出)
	@echo "\033[36m启动后端(FastAPI :8000) + 前端(Vite :5173)...\033[0m"
	@echo "\033[33m确保 PostgreSQL 已启动且已执行 make migrate\033[0m"
	@trap 'kill $$BACKEND_PID $$FRONTEND_PID 2>/dev/null; wait 2>/dev/null' INT TERM; \
	$(UV) run finboard serve --reload & BACKEND_PID=$$!; \
	cd $(WEB_DIR) && npm run dev & FRONTEND_PID=$$!; \
	wait

clean: ## 清理缓存与构建产物
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +
