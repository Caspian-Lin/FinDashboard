# FinDashboard

可实盘交易的模块化单体量化交易系统。设计目标见 [`phase1_doc.md`](./phase1_doc.md),
agent 必读约束见 [`AGENTS.md`](./AGENTS.md)。

> **当前阶段：研究回测里程碑收尾，QMT 真机验证暂缓。**
> 端到端实盘链路、研究数据/因子/无代码策略/统一 ResearchRun 和与实盘隔离的
> 持久化模拟盘均已建立；恢复实盘前仍须完成 QMT 权限和真机验证。

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
│   ├── finboard-simulation/       # 持久化隔离模拟账户 / 撮合 / 账本 / 恢复
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

CLI 入口(`uv run finboard --help`)提供 `run / serve / reconcile / migrate / kill-switch / worker` 等子命令。

### 6. 启动后台 Worker(统一任务队列,issue #117)

API 进程只做参数校验、创建任务和查询状态,所有研究/数据/回测域的耗时任务
(批量行情拉取、数据集发布、特征快照、回测、数据同步、ResearchRun 等)都由
独立的 **Worker 进程**从 PostgreSQL 持久化队列领取执行。开发期需另起一个终端:

```bash
uv run finboard worker run                # 默认:轮询全部队列,2s 间隔,最多 4 并发
uv run finboard worker run \
    --poll-interval 1.0 \
    --max-concurrent 8 \
    --queues research,data                # 只消费 research / data 队列
uv run finboard worker recover            # 仅回收过期 lease(running→interrupted),不常驻
```

不启动 Worker 时,提交端点仍会返回 `202 + job_id`,但任务会停留在 `queued`
直到 Worker 上线。Worker 崩溃后,过期租约由下次启动时的 `reclaim_stale` 自动
回收为 `interrupted`,可重新排队重放。Worker 不触及实盘下单/撤单/持仓/Kill Switch
——这些由交易内核专用 `finboard-scheduler` 执行,不进入统一队列。

---

## 统一后台任务队列(issue #117)

研究/数据/回测域的所有耗时任务统一走持久化 `background_jobs` 表(`BJ-` ID),
与实盘 `orders`/`fills`/`positions`/`audit_logs` 完全隔离、无外键。API 只
负责登记任务并立即返回,Worker 用 PostgreSQL `FOR UPDATE SKIP LOCKED` + 租约
+ 心跳从队列安全领取,支持多 Worker、API 重启、Worker 重启和进程恢复。

### 提交契约(202 + job_id)

以下端点提交后立即返回 **`202` + `JobOut`**(不在 HTTP 请求内等待计算):

| 端点 | kind | `result_ref` |
|------|------|--------------|
| `POST /api/data/bulk-download` | `bulk_download` | — |
| `POST /api/data/fetch-all` | `fetch_all` | — |
| `POST /api/data/sync` | `data_sync` | — |
| `POST /api/data/quality/repair` | `quality_repair` | — |
| `POST /api/instruments/datasets/releases` | `dataset_publish` | `release_id` |
| `POST /api/backtest/run` | `backtest_run` | `str(run_id)` |
| `POST /api/research/factors/features/jobs` | `feature_snapshot` | `snapshot_id` |
| `POST /api/research/runs` | `research_run` | `run_id` |

通用入口 `POST /api/jobs`(`kind=echo` 自检)同样返回 `202 + JobOut`;重复
`idempotency_key` 返回 `200` + 已有任务(幂等)。

### 查询与取消

```http
GET  /api/jobs?kind=bulk_download&status=running&queue=data&limit=100   # 列表(可重复参)
GET  /api/jobs/{job_id}                                                  # 单任务详情
POST /api/jobs/{job_id}/cancel                                           # 协作式取消
```

取消是**协作式**:`running → cancel_requested`,executor 在下一个 checkpoint
退出后置 `cancelled`;任务已在终态时返回当前状态不报错。Worker 崩溃/lease
过期时 `running → interrupted`,可重新排队重放。

### 任务状态机(8 态)

```
queued ─▶ running ─▶ succeeded
                  └▶ failed
                  └▶ retry_waiting ─▶ queued(重新入队)
running ─▶ cancel_requested ─▶ cancelled(协作式取消)
running ─▶ interrupted(worker 崩溃 / lease 过期)
```

`progress_done`/`progress_total`/`phase` 反映执行进度,`result_ref` 按上表
引用产物 ID(前端/Agent 终态后据此查详情),`error_code`/`error_summary` 记录
失败分类与摘要。同 `idempotency_key` 不会创建重复任务。

前端 / OpenCode Agent 拿到 `job_id` 后,用 `GET /api/jobs/{job_id}` 轮询直到
终态(`succeeded`/`failed`/`cancelled`/`interrupted`),再按 `result_ref`
取产物详情——刷新、切页或关闭浏览器后仍可凭 `job_id` 重新定位任务。

### 边界

- 统一队列只服务**研究/数据/回测**任务;实盘交易内核(盘前检查、收盘撤单、
  日终核对、Broker 心跳、Kill Switch)由专用 `finboard-scheduler` asyncio 调度
  执行,**不进入**统一队列、不暴露为 MCP 工具。
- Worker payload 不含 Tushare token、LLM API key 等敏感凭据。
- 回滚:关闭 Worker 进程即回退到不执行;`background_jobs` 表迁移可 downgrade。


---

## 策略参数与预设

控制台的“策略配置”页面只管理随应用发布、经过测试的内置策略。它不会上传或执行
Python 代码,保存预设也不会启动策略或修改实盘运行配置。

策略参数的单一真值来源位于
`packages/finboard-app/src/finboard_app/strategies/schema.py`:

1. 每个内置策略使用 Pydantic 模型声明参数类型、默认值、必填项、范围/枚举和跨字段约束。
2. `strategies/__init__.py` 的 `StrategyDefinition` 将策略类与参数模型、展示信息和
   `supports_backtest` 能力关联。
3. `GET /api/backtest/strategies` 自动把模型 JSON Schema 转换成前端表单契约;
   策略实例化、回测运行和预设保存复用同一模型校验。

新增内置策略时,只需新增参数模型并登记 `StrategyDefinition`,不要在 API 或 React
页面中按策略名称添加条件分支。未知字段、类型/范围错误和跨字段错误会以结构化
HTTP 422 返回。

策略预设保存在 PostgreSQL `strategy_presets` 表中,应用 `0004_strategy_presets`
迁移后可使用以下 API:

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/api/strategy-presets` | 列出预设 |
| `POST` | `/api/strategy-presets` | 校验并创建预设 |
| `GET` | `/api/strategy-presets/{id}` | 获取单个预设 |
| `PUT` | `/api/strategy-presets/{id}` | 校验并更新预设 |
| `DELETE` | `/api/strategy-presets/{id}` | 删除预设 |

预设还可保存独立的 `selection` 因子候选池配置。它不混入具体策略参数,因此同一套
过滤和排名规则可供不同内置回测策略消费。该配置默认关闭,保存或载入预设都不会
启动策略。

`POST /api/backtest/run` 接受同样的 `selection` 对象。响应及历史详情包含
`selection_snapshots`、聚合后的 `dataset_versions` 和 `factor_version`;
每个快照给出决策/生效日期、入选标的、发布或跳过状态、跳过原因和校验和。启用
选股但研究数据质量不足时会跳过当日调仓,不会使用部分数据。

### 静态候选池与策略层 Bar 规则选股

回测请求的 `symbols` 始终是**静态候选池硬上限**:

* **因子选股**(`selection`):由回测引擎在 T 日收盘后从研究数据库冻结 T+1 生效的
  候选池,需要数据契约、版本管理和质量校验。详见 `packages/finboard-backtest/README.md`。
* **Bar 规则选股**(`ma_cross` 策略的 `universe_*` 参数):策略内部消费已到达 Bar
  的研究级轻量过滤,无 I/O、不依赖研究数据,适合快速验证流动性/动量规则。

两者的输出都只能是静态 `symbols` 的子集,不能添加新标的或触发数据下载。Bar 规则
选股的参数随策略 schema 暴露,前端表单自动生成;默认 `universe_mode="all"` 保持
既有回测语义,显式切换为 `liquidity_momentum` 后才启用平均成交额 / 区间动量过滤,
并在 `universe_exit_clear=True` 时通过策略上下文对已有持仓发出卖出意图(仍走完整
风控链路)。行业、市值、基本面和跨截面排名不在 Bar 规则选股范围内,需要使用因子
选股。

在线代码编辑、动态模块导入和运行时执行用户代码不在当前能力范围内。此类能力必须
另行设计隔离与审批,并经过研究、回测、样本外、行情回放、模拟/影子交易和小资金
实盘验证,不能从网页直接进入实盘进程。

### 无代码研究策略规格

研究/回测域提供版本化 `ResearchStrategySpec`,把候选池、因子图、买卖/中性信号、
目标仓位、退出风控、成交假设和样本外验证计划放入一个闭环配置。#60-#64 与
`ma_cross` 均有统一模板;draft、publish、supersede、rollback 和结构化 diff 保留
只读历史。

该能力不是网页 Python 编辑器。API 只接受后端白名单因子、算子和参数,拒绝源码、
模块路径、模板与可执行表达式;保存或发布不会启动回测、模拟盘或实盘。完整 schema、
API、迁移和三个示例见 [无代码研究策略规格](docs/research_strategy_spec.md)。

### 版本化研究数据发布

回测可用 `finboard data release`，也可在研究工作台“数据与标的 → 数据发布”
中选择本地缓存的标的和日期范围，把可变 Parquet 缓存冻结为指定
`release_id`;`finboard data release-verify` 可复核 manifest 与全部文件
SHA-256。发布清单会登记到 PostgreSQL `research_dataset_releases`,API
`POST/GET /api/instruments/datasets/releases` 及详情端点提供多资产能力、
逐标的覆盖、`available_at` 和手数/T+N/税费/期货乘数与保证金快照。

发布采用 fail-closed 语义:必需资产能力、元数据、事件、覆盖或 checksum 任一
不完整就不登记可用版本,之前的发布保持不变;冻结 Provider 不会回退到可变缓存或
联网数据源。当前正式覆盖股票以及宽基/跨境/黄金/债券 ETF;可转债和期货只有在
合约元数据与生命周期事件完整时才允许发布。详见
[`packages/finboard-data/README.md`](./packages/finboard-data/README.md#不可变研究数据发布issue-77)。

### 因子实验室与风险模型

研究 API 现在提供版本化因子目录、不可变 `FeatureSnapshot`、alpha
`FactorSignal` 和可追溯实验记录。分析覆盖 Rank/Pearson IC、ICIR、分位收益、
显著性、衰减、换手/成本、参数邻域和市场状态;风险模型独立提供 beta、行业、
资产类别、规模、波动率、流动性、收缩协方差与风险贡献。利率/债券、汇率、黄金、
市场宽度和波动状态以 PIT 市场输入参与分层,不会直接成为买卖信号。

只有与 #57 样本外实验的真实持久化结果一致时,研究信号才可标为
`validated_oos`;网页或 API 不能靠声明 `passed_oos` 绕过验证。所有因子实验室
端点均为离线研究边界,不访问 Broker、账户、订单、持仓或实盘 Risk Manager,
也不提供 Python 策略编辑器。方法、接口和局限详见
[`packages/finboard-backtest/README.md`](./packages/finboard-backtest/README.md#因子实验室风险模型与跨市场特征issue-78)。

### 统一研究回测生命周期

`ResearchRun` 将冻结数据、因子快照、无代码策略规格、标准化信号、约束前后目标
仓位、风险退出状态、10/20/50 万可行性、离散调仓计划、研究订单/成交、成交驱动
持仓/盈亏和绩效报告保存为一条可递归查询的血缘。long-only 研究通过正式
`PortfolioPipelineAdapter` 生成这些阶段，不能由调用方预拼装目标/订单/持仓；
同版本重放会比较与 run ID 无关的结果 checksum。

单资产风险贡献上限现在是硬约束：只减小超限资产敞口，不重新放大其它资产；缺少
协方差、数学不可行或求解不收敛时，ResearchRun 以
`hard_constraint_rejected` 失败关闭且不生成研究订单 artifact。

运行状态覆盖 `queued / running / completed / failed / interrupted / rejected /
cancelled`。逐阶段 PostgreSQL checkpoint 支持重启恢复，run/decision/trace ID
可从成交回溯到候选池和冻结输入。研究表与实盘账户、订单、成交、持仓完全隔离，
持仓只能由研究成交推导。

API 只提供排队、历史、血缘、取消和重放登记，没有同步 `/run` 或 `/execute`
端点;LLM actor 在 API 和领域层都被拒绝。网页仍只编辑结构化策略，不提供 Python
策略代码。完整架构、状态机、artifact/API 契约、错误语义、复现步骤和
`phase1_doc.md` §3.4 映射见
[统一研究回测生命周期](docs/research_run_lifecycle.md)。

固定样本复现:

```bash
uv run pytest tests/unit/research_run tests/unit/test_api_research_runs.py -v
uv run pytest tests/integration/test_research_run_persistence.py -v
```

### 持久化产品模拟盘

已发布且完成机器验证的无代码策略可以进入独立产品模拟盘。模拟账户、会话、决策、
订单、成交、持仓、账本、行情事件和审计均使用 `SIM-*` ID 与 `simulation_*` 表；
后端不导入 Broker/QMT/CTP，也不提供直接创建订单端点。目标仓位必须引用会话批准
的 `ResearchRun` 和真实 signal trace，再经过模拟风险预占、next-bar 撮合和
成交驱动记账。

它与 `MockBroker` 的用途不同：MockBroker 用于开发/CI 验证实盘内核接口，状态以
测试场景为中心；产品模拟盘面向可恢复的长期会话，冻结策略/数据版本，持久化市场
时钟、资金、持仓和审计，并提供独立 REST/WebSocket 查询。模拟结果不代表未来收益，
`eligible` 只是一条审计状态，不会自动启动影子盘或实盘。

架构、撮合与资产假设、换月规则、恢复流程、API 契约和操作步骤见
[持久化模拟交易环境](docs/simulation_trading.md)。

### 配置项说明（InfoHint）

回测、行情数据、设置和策略配置页使用
`web/src/components/InfoHint.tsx` 展示就地说明。公共说明文案集中在
`web/src/lib/infoHints.ts`;后端 schema 生成的策略参数则根据字段描述和约束自动
生成说明。

新增说明时遵循以下约定:

1. 使用 `InfoHint` 展示术语说明,或使用 `HintLabel` 将表单标签、控件和说明关联;
   不在页面中复制自定义 tooltip 定位逻辑。
2. `title` 使用用户看到的术语,`description` 说明作用与操作逻辑,`detail` 补充单位、
   典型范围或安全边界。典型值是示例而不是投资或交易建议。
3. 触发器必须保留可访问名称和原生 `button` 键盘行为。组件支持 hover、focus、
   Enter/Space、Escape 和 click/touch,浮层通过 portal 渲染以避免被滚动容器裁剪。
4. 说明只用于展示,不得在打开/关闭时修改字段、提交表单、启动策略或触发交易动作。
5. 长文案应保持简洁;需要多段操作指南时使用页面正文或文档,不要把 tooltip 变成
   可交互面板。

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

## OpenCode 研究运行时(issue #108 / #109 / #118 / #122 / #157)

FinBoard 把 [OpenCode](https://opencode.ai) 作为受控研究 Agent 运行时,通过
`finboard-mcp` 受控工具层访问研究能力。OpenCode 是**研究域**入口(查询 + 研究
写操作自主执行),不连接实盘 broker / 账户 / 订单 / 持仓 / Kill Switch。

### 架构

```
研究用户(单一用户模型,无鉴权)
  ↓ http://127.0.0.1:4097
opencode web Docker 容器(宿主机侧锁 loopback)
  ↓ finboard-researcher agent(内置 bash/edit/write/webfetch 等默认拒绝,
    只放行 read/glob/grep/skill + finboard_* MCP 工具)
finboard-mcp(受控 MCP 工具,117 个;研究写操作自主执行 #122)
  ↓ Bearer token(streamable-http)
FinDashboard service / repository(唯一事实来源)
```

### 启动与接入(推荐:Web 容器模式)

```bash
# FinBoard 托管 opencode web Docker 容器(默认 4097)+ 内嵌 finboard-mcp HTTP server(8765):
FINBOARD_OPENCODE_WEB_ENABLED=true \
FINBOARD_OPENCODE_MANAGE_PROCESS=true \
FINBOARD_OPENCODE_WEB_CORS_ORIGINS=http://localhost:5173 \
FINBOARD_MCP_AUTH_TOKEN=<token> \
FINBOARD_LLM_API_KEY=<key> \
FINBOARD_OPENCODE_ENV_OVERRIDES=DEEPSEEK_API_KEY=<key> \
uv run uvicorn finboard_api.app:app
```

要点(#157):
- **无 basic auth**:单用户模型下 OpenCode Web 直接使用明文
  `http://127.0.0.1:4097`,`/api/opencode/access` 只签发 `web_url`;
  宿主机侧 `127.0.0.1` 绑定是唯一网络边界。
- **MCP 地址可配置**:`FINBOARD_OPENCODE_MCP_REMOTE_URL` 在容器启动时渲染进
  `.opencode/runtime/opencode.json` 并以单文件 bind mount 覆盖容器内配置;
  内嵌 MCP 在容器模式下默认绑 `0.0.0.0`(Bearer token 保护)。
- 也可用 Makefile 独立跑 MCP HTTP server:`make mcp-serve-http MCP_AUTH_TOKEN=<token>`。

外部 serve 模式(向后兼容,`opencode serve` 4096 + `FINBOARD_OPENCODE_ENABLED=true`)
仍可用,见 `packages/finboard-opencode` 文档。

### 隔离保证(Docker 容器)

- **容器隔离**:版本锁定镜像 + 独立命名卷(`opencode-data` 会话 DB / auth,
  容器删除后保留),与宿主机全局 opencode 彻底分离;
- **环境变量严格白名单**:容器只注入 LLM Key / MCP token / basic auth 凭证,
  **绝不**继承 FinBoard 的 DB 密码 / broker 凭证 / API Key;
- **网络**:宿主机侧强制 `127.0.0.1` 绑定,不暴露公网;容器经
  `host.docker.internal` 访问宿主机 finboard-mcp;
- **项目配置 bind mount**:`.opencode`(agent 定义 / opencode.json)与
  `.agents`(研究 Skill)只读挂载进容器。

### 网关端点(`/api/opencode`)

- `GET /status` —— 容器运行状态 + 内嵌 finboard-mcp server 运行状态(#157);
- `GET /health` —— 代理健康探测;
- `POST /access` —— 签发明文 `web_url`(#157 后无凭证字段;#121 后不绑定
  conversation),前端 iframe 与「新窗口打开」共用同一 URL。

### 前端研究工作台(issue #111 / #121 / #122)

`/research/workbench` 两个 Tab:

- **工作台**:iframe 直连 OpenCode Web(OpenCode 自身管理会话 / 历史 / 恢复,
  FinBoard 不再维护独立会话投影层);顶部 banner 显示 OpenCode Web 与 FinBoard MCP
  运行状态;**研究写操作(创建 ResearchRun / 回测 / 模拟盘)由 agent 通过 MCP
  自主执行(#122)**,不设网页审批硬门。
- **审批中心**:AI 草案 / 因子假设 / 审计日志的审计与历史查看入口(走 REST
  `/api/research/ai/*`,不依赖 OpenCode Web 网关)。

**降级**:`opencode_web_enabled=false`(网关返回 503)时「工作台」Tab 显示降级
提示,「审批中心」Tab 仍可用。

### MCP 审计与工具契约

- 每次 MCP 工具调用记录审计事件(structlog + 内存副本;`FINBOARD_MCP_AUDIT_PERSIST=true`
  时追加到独立 `mcp_audit_events` 表,`GET /api/mcp/audit` 可查询,#157);
- 工具清单与边界同步规范见 #123:`_INSTRUCTIONS` / Skill `SKILL.md` /
  `references/tools.md` / `ROADMAP.md` 必须与注册表同步,契约测试锁定工具总数。

### 回滚

- 关闭 MCP 入口:`FINBOARD_MCP_ENABLED=false`(默认)
- 关闭外部 serve 接入:`FINBOARD_OPENCODE_ENABLED=false`(默认)
- 关闭 Web 工作台网关:`FINBOARD_OPENCODE_WEB_ENABLED=false`(默认),网关端点
  返回 503、不启动容器
- 审计持久化:`FINBOARD_MCP_AUDIT_PERSIST=false`(默认)+ 迁移 downgrade
- 均不影响现有 ResearchAssistant / REST 入口与研究产物。

---

## Git 工作流

详见 `AGENTS.md` 的 *Git 工作流* 与 *Issue / PR 规范* 章节。简要:

- 从 `dev` 切 `feat/<issue-slug-id>`,本地通过 CI 后用 `gh pr create` 提 PR
- **合并 PR 是唯一需要用户确认的步骤**
- 提交信息格式 `<分类>: <修改点描述>`,禁用任何 `Co-Authored-By` 署名

---

## License

Proprietary. 内部使用,未获得授权不得外传。
