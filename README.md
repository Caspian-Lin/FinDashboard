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

回测可先用 `finboard data release` 把可变 Parquet 缓存冻结为指定
`release_id`,再用 `finboard data release-verify` 校验 manifest 与全部文件
SHA-256。发布清单会登记到 PostgreSQL `research_dataset_releases`,API
`GET /api/instruments/datasets/releases` 及其详情端点提供多资产能力、逐标的
覆盖、`available_at` 和手数/T+N/税费/期货乘数与保证金快照。

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
仓位、离散调仓计划、研究订单/成交、成交驱动持仓/盈亏和绩效报告保存为一条可递归
查询的血缘。#29 与 #60-#64 通过同一个适配器和结果契约进入编排器;同版本重放会
比较与 run ID 无关的结果 checksum。

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

## Git 工作流

详见 `AGENTS.md` 的 *Git 工作流* 与 *Issue / PR 规范* 章节。简要:

- 从 `dev` 切 `feat/<issue-slug-id>`,本地通过 CI 后用 `gh pr create` 提 PR
- **合并 PR 是唯一需要用户确认的步骤**
- 提交信息格式 `<分类>: <修改点描述>`,禁用任何 `Co-Authored-By` 署名

---

## License

Proprietary. 内部使用,未获得授权不得外传。
