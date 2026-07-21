# FinDashboard

可实盘交易的模块化单体量化交易系统。设计目标见 [`phase1_doc.md`](./phase1_doc.md),
agent 必读约束见 [`AGENTS.md`](./AGENTS.md)。

> **当前阶段:P0 — 打通端到端真实交易链路。** 任何超出 P0 范围(TWAP/VWAP、因子系统、
> LLM 接入等)的功能都暂不开发,详见 `phase1_doc.md` §13 阶段优先级。

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
| API 后端 | FastAPI(P3 之后启用) |
| 前端 | React + Vite + TypeScript(P3 之后启用) |
| 测试 | pytest + pytest-asyncio |
| 静态检查 | ruff(lint+format)、mypy(strict) |
| CI | GitHub Actions(lint + typecheck + unit test) |

---

## 仓库结构

```
FinDashboard/
├── packages/                      # uv workspace 成员
│   ├── finboard-shared/           # 跨模块的枚举、数据模型、ID 类型
│   ├── finboard-broker/           # BrokerAdapter 抽象 + Mock 实现
│   ├── finboard-broker-qmt/       # QMT (xtquant) 实现
│   ├── finboard-broker-ctp/       # CTP (期货) 实现
│   ├── finboard-persistence/      # SQLAlchemy ORM + Repository
│   ├── finboard-core/             # 事件总线 / 订单 / 持仓 / 账户 / 状态机
│   ├── finboard-risk/             # 下单前检查 / Kill Switch
│   ├── finboard-reconcile/        # 本地 ↔ 券商核对
│   └── finboard-app/              # 进程入口 + CLI + 配置
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
- Docker(本地起 PostgreSQL)

### 1. 安装依赖

```bash
make install          # 等同于 uv sync --all-packages
```

### 2. 启动本地 PostgreSQL

```bash
cp .env.example .env
make db-up
make migrate
```

### 3. 跑测试与静态检查

```bash
make lint
make typecheck
make test
```

### 4. 启动交易核心(默认 Mock Broker)

```bash
make run              # 等同于 uv run finboard run
```

CLI 入口(`uv run finboard --help`)提供 `run / reconcile / migrate / kill-switch` 等子命令。

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
