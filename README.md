# FinDashboard

可实盘交易的模块化单体量化交易系统。设计目标见 [`phase1_doc.md`](./phase1_doc.md),
agent 必读约束见 [`AGENTS.md`](./AGENTS.md)。

> **当前阶段:P2 — 重启恢复 + 行情接入 + 策略运行器 + 人工交易控制台已完成。**
> 端到端真实交易链路(MockBroker + QMT)、行情数据接入、策略 ABC 框架、
> FastAPI REST + WebSocket API + React 前端控制台均已就绪。

---

## 技术栈

| 维度 | 选型 |
|------|------|
| 语言 / 运行时 | Python 3.12 |
| 包管理 / monorepo | [uv](https://github.com/astral-sh/uv) workspace |
| 形态 | 模块化单体(asyncio 全异步单进程) |
| 持久化 | PostgreSQL 16 + SQLAlchemy 2.0 + Alembic |
| 日志 / 可观测性 | structlog(JSON) |
| Broker 接口 | QMT(A股 / xtquant)、CTP(期货)、Mock(本地/CI) |
| API 后端 | FastAPI + WebSocket(目录 `packages/finboard-api/`) |
| 前端 | React 19 + Vite + TypeScript + Tailwind CSS + TanStack Query(目录 `web/`) |
| 测试 | pytest + pytest-asyncio |
| 静态检查 | ruff(lint+format)、mypy(strict) |
| CI | GitHub Actions(lint + typecheck + unit test) |

---

## 仓库结构

```
FinDashboard/
├── packages/                      # uv workspace 成员
│   ├── finboard-shared/           # 跨模块的枚举、数据模型、ID 类型
│   ├── finboard-broker/           # BrokerAdapter / MarketDataAdapter 抽象 + Mock 实现
│   ├── finboard-broker-qmt/       # QMT (xtquant) 交易 + 行情实现
│   ├── finboard-broker-ctp/       # CTP (期货) 实现
│   ├── finboard-persistence/      # SQLAlchemy ORM + Repository
│   ├── finboard-core/             # 事件总线 / 订单 / 持仓 / 账户 / 状态机 / 策略运行器
│   ├── finboard-risk/             # 下单前检查 / Kill Switch
│   ├── finboard-reconcile/        # 本地 ↔ 券商核对 + 重启恢复
│   ├── finboard-app/              # 进程入口 + CLI + 配置 + 策略工厂
│   └── finboard-api/             # FastAPI REST + WebSocket API(人工交易控制台后端)
├── web/                           # React 前端(Vite + Tailwind + TanStack Query)
├── migrations/                    # Alembic 迁移脚本
├── tests/                         # 跨包测试(unit / integration)
├── docker/                        # 构建镜像
├── .github/                       # issue / PR 模板 + CI
├── pyproject.toml                 # workspace 根 + 工具配置
├── alembic.ini
├── Makefile
└── phase1_doc.md / AGENTS.md
```

---

## 快速开始

### 0. 前置

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Node.js 18+(前端开发)
- PostgreSQL 16+(任选其一):
  - 本机已装 PostgreSQL(推荐,启动快)
  - Docker / Docker Desktop(`make db-up` 用)

### 1. 安装依赖

```bash
make install          # 后端: uv sync --all-packages
make web-install      # 前端: cd web && npm install
```

### 2. 准备数据库

#### 方式 A:本机已装 PostgreSQL(推荐)

用 `postgres` 超级用户创建专用账户与库:

```bash
sudo -u postgres psql <<'SQL'
CREATE USER findashboard WITH PASSWORD 'CHANGE_ME';
CREATE DATABASE findashboard
    OWNER findashboard
    ENCODING 'UTF8'
    LC_COLLATE 'C.UTF-8'
    LC_CTYPE 'C.UTF-8'
    TEMPLATE template0;
GRANT ALL PRIVILEGES ON DATABASE findashboard TO findashboard;
\c findashboard
GRANT ALL ON SCHEMA public TO findashboard;
ALTER SCHEMA public OWNER TO findashboard;
SQL
```

把 `CHANGE_ME` 换成强密码,然后填入 `.env`(见下)。

#### 方式 B:docker compose

```bash
make db-up            # 起 postgres:16 容器,用户/密码/库默认 findashboard
```

#### 配置 `.env`

```bash
cp .env.example .env
# 编辑 .env,把 FINBOARD_DB_URL 里的 CHANGE_ME 替换为上面设置的密码
```

### 3. 应用迁移 + 跑测试

```bash
make migrate          # alembic upgrade head
make lint             # ruff check
make typecheck        # mypy strict
make test             # unit + smoke(默认跳过 integration)
make test-integration # 需要 PostgreSQL 已就绪
```

### 4. 一键启动前后端开发服务器

```bash
make dev              # 后端 FastAPI(:8000) + 前端 Vite(:5173),Ctrl-C 同时退出
```

打开浏览器访问 `http://localhost:5173` 即可使用交易控制台。
Vite dev server 自动代理 `/api` 和 `/ws` 到后端。

也可以分别启动:

```bash
make serve            # 仅后端(带 --reload 热重载)
make web-dev          # 仅前端
```

### 5. 启动交易核心(CLI 模式,不带 API)

```bash
make run              # 等同于 uv run finboard run
```

CLI 入口(`uv run finboard --help`)提供 `run / serve / reconcile / migrate / kill-switch` 等子命令。

---

## QMT(迅投 xtquant)实盘接入

> **仅 Windows + miniQMT 环境**。Linux/macOS/CI 使用 mock broker。

### 前置条件

1. 安装 QMT 客户端(券商提供),启动 **miniQMT** 模式并登录资金账号
2. 确认 `xtquant` 可导入:QMT 安装目录下的 `userdata_mini` 文件夹包含 `xtquant` 包
3. 将 `userdata_mini` 路径加入 `PYTHONPATH`,或安装 `xtquant` 到 Python 环境

### 配置

编辑 `.env`:

```ini
FINBOARD_BROKER=qmt
FINBOARD_ACCOUNT_ID=12345678        # QMT 资金账号
FINBOARD_QMT_PATH=C:\QMT\userdata_mini  # userdata_mini 完整路径
FINBOARD_QMT_SESSION_ID=1            # 会话号,多进程须唯一
```

### 运行

```bash
# Windows PowerShell / cmd
uv run finboard run           # 启动 → 连接 → 查询 → reconcile → 等待 Ctrl-C
uv run finboard reconcile     # 单独执行一次本地 ↔ 券商核对
```

### A 股交易规则提醒

- **T+1**:当日买入的股票次日才能卖出;系统通过 `available_quantity`(券商查询)自动校验
- **最小单位**:买入须为 100 股整数倍;卖出可不足 100 股(零股)
- **价格变动单位**:A 股 0.01 元;ETF 0.001 元
- **涨跌停**:超出涨跌停价的订单会被券商拒单
- **集合竞价**:9:15-9:25 / 14:57-15:00,不支持撤单

### session_id 选取规则

`session_id` 是 xtquant 用来区分不同策略进程的标识:

- 同一台机器上同时运行多个进程时,每个进程用不同的 `session_id`
- 单进程重启可以复用同一 `session_id`
- 取值范围:正整数(建议 1-999)

---

## 交易链路

```
Strategy (P5) → Order Intent
        ↓
Risk Manager (下单前检查 + Kill Switch)
        ↓
Order Manager (本地订单状态机 + client_order_id 唯一性)
        ↓
Broker Adapter (QMT / CTP / Mock)
        ↓
券商 / 交易所
        ↓
回报(委托/成交/拒单) → 更新订单 → 更新持仓/资金 → Reconcile
```

**交易安全红线**(改动须先与用户确认,详见 `AGENTS.md`):

- `client_order_id` 必须本地生成且全局唯一
- 下单请求超时禁止无条件重试,须先进入 `UNKNOWN` 再查券商确认
- 持仓的真实来源是券商查询,禁止策略直接改持仓
- 重启后须先完成核对,通过前禁止发新单
- Kill Switch 由交易内核执行,不能只依赖网页按钮

---

## Git 工作流

详见 `AGENTS.md` 的 *Git 工作流* 与 *Issue / PR 规范* 章节。简要:

- 从 `dev` 切 `feat/<issue-slug-id>`,本地通过 CI 后用 `gh pr create` 提 PR
- **合并 PR 是唯一需要用户确认的步骤**
- 提交信息格式 `<分类>: <修改点描述>`,禁用任何 `Co-Authored-By` 署名

---

## License

Proprietary. 内部使用,未获得授权不得外传。
