# AGENTS.md

可实盘交易的量化系统。实盘交易涉及真实资金，改动须谨慎，不要在未确认风险的情况下改动下单 / 持仓 / 风控相关逻辑。

## 命名约定

- **全名**:`FinDashboard`(大写 F、D、S)—— 用于文档标题、对外说明、正式表述。
- **缩写**:`finboard`(全小写)—— 用于包目录名(`packages/finboard-*`)、Python 模块名(`finboard_*`)、CLI 命令(`finboard`)、内部叙述。
- **禁止变体**:`Findashboard`、`FINBOARD`(全大写,除非是环境变量前缀 `FINBOARD_`)、`Finboard`(仅首字母大写)等。
- **历史遗留标识符(保持不变,不视作违规)**:
  - `pyproject.toml` 的 `name = "findashboard"`(分发包名,改影响大);
  - PostgreSQL 默认库名 / 用户名 `findashboard`(用户已按此建库,见 `README.md` 数据库初始化);
  - 前端 `web/package.json` 的 `name = "findashboard-web"`、localStorage key `findashboard-theme`(已发布的标识符)。
  这些是技术标识符,不是叙述用词;新代码 / 新文档应遵循上方的全名 / 缩写约定。

## 项目设计

完整设计文档见 `phase1_doc.md`（生产级量化交易系统开发计划）。以下为对 agent 最关键的红线与约束。

### 架构选型（第一阶段硬约束）
- 技术栈：Python 交易进程 + PostgreSQL
- 形态：**模块化单体**；不引入微服务 / Kafka / Kubernetes / 分布式事务
- 范围：单账户、单券商接口、单市场（A股或期货二选一）；在链路稳定前不抽象多券商适配层
- 模块划分：Broker Adapter / Trading Gateway / Order Manager / Position Manager / Account Manager / Risk Manager / Strategy Runner / Reconciliation

### 当前阶段（2026-09-13）
- P0-P4 已完成（issue #1-#14 关闭）；P5 大部分完成；研究里程碑收尾完成——研究数据发布 / 因子实验室 / 无代码策略规格 / 统一 `ResearchRun` / 组合风控流水线 / 持久化产品模拟盘均已建立。
- QMT 实盘验证搁置（#22/#24/#26）。当前主线 = 研究数据链路（dataset_sync / 冻结发布）、回测与研究运行、因子平台（factor_series / 因子批次）、性能批次。
- **实盘恢复条件**：QMT 权限解决 → #22 真机验证 → #23 标的元数据 → #24 连续运行 → 小资金实盘。
- **P6 及以后禁止提前开工**：TWAP / VWAP / 冰山单 / 智能拆单、跨账户路由、复杂回测、LLM 接入（因子系统已经用户批准落地）。
- **逐 issue 完成记录不在本文件**：实现细节、事故取证与 Know-how 查 GitHub（`gh issue view N` / `gh pr view N` / `gh pr list --search`）——本仓库 PR 正文按规范含「关键决策 / Know-how」；本文件的 git 历史（2026-09-13 之前版本）也保留全部逐 issue 长记录可考古。

### 平台关键不变量（写代码前速查；完整语义以代码与测试为准）
- **域隔离**：研究运行（`research_runs`，`RR-`）与模拟盘（`simulation_*`，`SIM-`）永不写实盘 `orders`/`fills`/`positions`/`audit_logs`；`background_jobs` 队列只服务研究/数据/回测，实盘交易内核走 `finboard-scheduler` 专用调度、不进队列不暴露为 MCP 工具。
- **benchmark-only**：指数与期货主连不可撮合、只做基准/研究数据；`is_benchmark_only_instrument` 必须在候选池、特征装配、排名截面三处消费口径一致（漏一处 = 指数被交易或信号截面漂移）。
- **bars 主发布唯一**：基准行情必须与候选池同处一份 `multi_asset_mixed` 发布；研究数据发布（daily_metrics / financial_indicators / convertible_metrics）只提供因子观测——缺研究标的容忍（具名 warning），bars 主发布缺标的 fail-closed。
- **PIT fail-closed 上界**：一切决策输入 `available_at <= decision_at`；财务数据 `available_at = ann_date + 1`；窗口外/前视数据具名拒绝、不静默截断。
- **multi_period 必须显式 `decision_schedule`**（legacy `rebalance_frequency` 兼容，两键互斥）；single_shot 决策时点只能来自冻结因子快照（入队 fail-closed）。
- **用户因子（`u_` 前缀）**：仅 active+passed 可引用；multi_period 引用须有 factor_series 覆盖；factor_series 仅接受 `compute_series` 入口（v1 逐日入口已废弃），PIT = 窗口物理隔离 + 前缀不变性审计（截断不变 + 扰动不变）。
- **晋级链不放松**：研究 → 回测 → OOS(#57) → 模拟 → 影子 → 小资金；screen 门 rank_ic 取绝对值（只证信号存在性，方向责任在因子 preference 与权益复测）；读验证结论先看 `oos_outcome` 再看 `status`（OOS 流程完成 ≠ 假设获支持）。
- **组合硬约束 fail-closed**：`max_risk_contribution` 默认 0.35，不可行必须在研究订单前失败关闭；入队预检与运行期双重防线；`portfolio_config`（组合约束）与 `risk_config`（风险退出）两分区键位不可混。
- **幂等键部分唯一索引**：failed/cancelled 放行同键重建，succeeded 幂等命中，interrupted 保持单行（lease 重排与 run replay 依赖此语义）。
- **checksum / manifest 漂移防护**：新增可选字段 None 省略键，保持旧 payload 字节稳定；语义变更走 `schema_version`；禁止直接改既有 `as_dict` 已有字段的输出。
- **数据单位口径（对账锁定，改动 = 口径事故）**：股票 daily `amount` 千元；期货 `fut_daily` amount 万元→元 ×10000、vol 手→张；可转债 vol 手→张 ×10；指数 amount 千元↔元按源对齐。
- **迁移 revision id 全局唯一不复用**（撞号先例：c1d2e3f4a5b6 已被 #215 占用）。

### 研究治理边界
- **正式组合入口（#91）**：long-only `ResearchRun` 必须由 `PortfolioPipelineAdapter` 从冻结输入生成目标 / 硬约束 / 风险退出 / 三档资金可行性 / 研究订单与账本；风险贡献上限启用后缺少协方差、数学不可行或不收敛必须在生成研究订单前失败关闭。
- **产品模拟盘（#83）**：只接受已发布策略与 completed `ResearchRun` 的结构化目标仓位；禁止导入 Broker/QMT、直接创建订单或修改持仓、自动晋级影子盘/实盘；订单/成交/持仓/资金/审计全部写独立模拟表。
- **web 通道无代码**：允许版本化无代码规格组合白名单候选池/因子/信号/风控；禁止网页提交 Python、模块路径或可执行表达式；保存/发布配置不得自动启动任何运行。
- **MCP 代码通道（#215/#216）**：agent 经 `finboard_research_code_submit` 受控提交策略/因子 Python 代码（manifest/入口签名/import 白名单/禁 subprocess·socket·文件写·eval/文件数与大小上限/拒二进制），只存储与版本化；执行只经一次性 Docker 沙箱（`--network none` / 只读根 / cap-drop ALL / 非 root / 资源与墙钟限额；PIT = 物理隔离 + 挂载生成端 fail-closed）。
- **沙箱产物消费（#217/#218）**：输出过质量门（NaN 比例、覆盖率阈值）才落 `factor_feature_snapshots`，因子名 `u_<artifact>` 与内置目录隔离；user_code 策略逐决策容器执行、只出目标权重不触任何订单语义。
- **晋级生命周期（#219/#233/#234）**：
  ```text
  submit -> draft(pending) -- screen + #57 OOS --> active(passed)
                         \-- gate failed --------> draft(failed)
  active(passed) -- 新版本晋级或显式退役 --> retired
  rollback(old commit) -----------------------> draft(pending)
  active(passed) -> 正式 composite / simulation whitelist
  ```
  OOS 执行走 `validation_experiment` 后台任务，揭盲一次性不可重做；screen 用 draft 产物须显式绑定（`screen_artifact_bindings`）；晋级与沙箱运行归档 code commit × 数据 release × 参数 × output checksum 四向引用。

### LLM / Agent 边界
LLM（包括本 agent 自身）**不允许**：直接连接实盘账户、直接发送订单、修改账户持仓、绕过风控、在实盘运行时动态生成代码并立即执行。LLM 的产出必须经过完整流程才可上实盘：`研究 → 回测 → 样本外 → 行情回放 → 模拟交易 → 影子交易 → 小资金实盘 → 扩大资金`。

**#160 起 FinBoard 不再内置任何 LLM 调用**：`ResearchAssistant`/`OpenAICompatibleLLMProvider`/`llm_*` 配置与 `finboard.ai.*` MCP 工具已整体移除，AI 问答、假设与策略草案由 OpenCode 运行时的外层 LLM 直接承担。FinBoard 侧的安全控制收敛为：MCP 工具入参统一 `sanitize_prompt` 脱敏 + 审计（`mcp_audit_events`），研究写操作由 agent 自主执行（#122），实盘能力永久不注册为工具。`factor_research` 包仅保留 `sanitizer.py`。

### FinBoard MCP Server 与 OpenCode 运行时边界
- **MCP Server（#108）**：研究能力以受控 MCP 工具暴露给外置 Agent，FinDashboard 仍是唯一事实来源；研究写操作（创建快照/策略/运行/发布/启动模拟盘）agent 可自主执行（#122）；实盘能力（下单/撤单/改持仓/Kill Switch/连接 broker/凭证探测）**永久不注册为工具**；工具返回统一信封 `ToolEnvelope`；入参 `sanitize_prompt` 脱敏 + 审计（structlog + `mcp_audit_events` 表，REST `GET /api/mcp/audit` 可查），不记录凭证与未脱敏内容。
- **任务队列 MCP（#136）**：`finboard_job_*` 只覆盖研究/数据/回测类 kind（`background_jobs`，`BJ-` ID）；`job_cancel` 是研究域协作式取消，非实盘撤单。
- **能力同步规范（#123，硬性）**：新增/修改研究功能必须同步 ①`finboard-mcp` 注册/更新工具 ②`server.py` 的 `_INSTRUCTIONS` ③Skill `references/tools.md` ④（涉及新阶段时）`packages/finboard-mcp/ROADMAP.md`——否则外置 agent 认知与研究现实脱节。
- **OpenCode 运行时（#109/#118）**：`opencode web` 容器级隔离（独立命名卷持久化会话与 auth、版本锁定镜像、环境变量白名单，**绝不**继承 FinBoard 的 DB 密码/broker 凭证/API Key）；FinBoard 网关只是控制面（status/health/access），不透传流量；单用户模型下宿主机 127.0.0.1 绑定是唯一网络边界（无 basic auth，多用户需恢复网关鉴权）。容器经 `host.docker.internal:8765` 访问内嵌 `finboard_mcp`（streamable-http + Bearer）；**lifespan 必须先起内嵌 MCP、后建容器**（opencode 首连失败即 `failed` 且不再重试）；`HOME=/workspace` 与 XDG 目录注入语义不可变（决定文件选择器与数据落点）。
- **agent 权限 allowlist**：`read`/`glob`/`grep`/`skill`/`bash`（仅只读轮询/状态检查）/`finboard_*`/`exa_*` 放行；`edit`/`write`/`webfetch` 拒绝；`.opencode`/`.agents`/docs 挂载一律 `:ro`。**agent 定义唯一权威 = `.opencode/agent/*.md`**（opencode.json 的 agent 块是死配置）；官方容器无 JS 运行时、local MCP 不可行，搜索走 remote exa 端点（#313 派生镜像补 jq+python3；切换镜像须 `docker rm -f finboard-opencode-web` 重建）。
- **研究记忆与知识沉淀（#110/#268）**：研究长期记忆持久化在 `research_memories` 表（`finboard.memory.*` 工具），只引用产物不修改，`created_by` 不可伪造；跨会话研究知识 canonical = `docs/research/`（ROADMAP / FINDINGS / rounds，PR 维护、容器只读挂载），**文档与记忆冲突以文档为准**；会话启动/收尾协议与「未查结论索引禁止重测已证伪假设」禁令见 Skill `references/workflow.md` 与 `references/memory.md`。

## Git 工作流

### 分支模型
- `main` — 稳定发布
- `dev` — 集成分支，所有新特性从这里切出
- `m/<里程碑>` — 里程碑分支
- `feat/<issue-slug-id>` — 功能分支，从 `dev` 切出

### 开发流程（严格按序）
0. 在github仓库创建issue和对应里程碑（可选）
1. 从最新的 `dev` 切出 `feat/<issue-slug-id>`
2. 本地完成开发，**本地须通过 CI**
3. 用 `gh pr create` 创建 PR（目标分支为对应里程碑或 `dev`）
4. **等待用户确认合并** — 这是整个流程中唯一需要用户确认的步骤
5. PR 合并后会自动关闭对应 issue，不要手动关

feat 分支只能通过 PR 合并进里程碑或 `dev`，禁止直接 push 合并。

### 多 worktree 并行开发（多 agent，#289）

主目录是第一个 agent 的常驻工作区；更多 agent 各用独立 git worktree，禁止共用
工作目录、禁止在他人工作区 checkout / merge 对方分支、禁止 checkout 别的 worktree
已持有的分支（git 本身会拒绝）。昂贵数据（`data_cache` ≈680MB / `data_releases`
≈2.0GB）只存主目录一份，worktree 以 NTFS junction 共享，重新拉取不可接受。

**一键创建（推荐，幂等可重跑）**：

```bash
scripts/create-worktree.sh <N> <feat分支名> [base=origin/m/<里程碑>]
# 例: scripts/create-worktree.sh 3 feat/my-issue-290 origin/m/research-backtest
```

脚本依次完成：worktree add（主目录同级 `FinDashboard-wtN`）→ junction 共享
`data_cache` / `data_releases` → 复制并改写 `.env`（独立库 + 端口 + 关闭
OpenCode/MCP）→ 建库 `findashboard_wtN` / `findashboard_wtN_test` →
`uv sync`（复用主目录 `.uv-cache`）→ `alembic upgrade head` → `web npm install`。

**手工等价步骤与硬性规则**：

- **数据共享**：`cmd //c "mklink /J data_cache <主目录绝对路径>\data_cache"`
  （`data_releases` 同理）。**禁止 `ln -s`**（Git Bash 默认是复制而非链接）；
  **禁止对 junction 执行 `rm -rf`**（Git Bash 会穿透链接删掉主目录真实数据），
  删除链接只能 `cmd //c rmdir <名字>`。
- **隔离配置**：pydantic 按 CWD 读 `.env`，每 worktree 一份——`FINBOARD_DB_URL` /
  `FINBOARD_TEST_DB_URL` 指向 `findashboard_wtN` / `findashboard_wtN_test`；
  `FINBOARD_OPENCODE_ENABLED` / `FINBOARD_OPENCODE_WEB_ENABLED` /
  `FINBOARD_MCP_ENABLED` 置 false——OpenCode / MCP / 研究运行时全局锚定主目录
  （容器名与端口全局唯一，第二个实例会撞名）。
- **端口分配（wtN，N≥2）**：API `8000+N`、前端 `5172+N`、MCP（如启用）`8763+N`；
  启动用 `make dev API_PORT=8002 WEB_PORT=5174`（vite 端口 / API 代理目标支持
  `FINBOARD_WEB_PORT` / `FINBOARD_WEB_API_PORT` 覆盖；显式指定端口被占用即报错，
  不静默换端口——静默换端口后代理会指到别的 worktree 的后端，预览到错误数据）。
- **依赖**：`UV_CACHE_DIR=<主目录>/.uv-cache uv sync --all-packages` 一次性复用
  主目录下载缓存；`web/node_modules` 各 worktree 独立 `npm install`，不共享
  （两端 install 会互相踩）。测试库无需 alembic（conftest `ensure_test_db` +
  `create_all` 自动建表），开发库须 `uv run alembic upgrade head`。
- **纪律**：进 worktree 先切 feat 分支再动代码，不直接在 `m/*` 分支提交；pytest
  仍在各 worktree 根目录运行（`--basetemp .pytest-tmp` 相对各自根，见下文
  Windows ACL 约定）；PR / issue 流程与单工作区完全一致。
- **回收**：先 `cmd //c rmdir` 两个 junction，再 `git worktree remove
  ../FinDashboard-wtN`；数据主体在主目录，删除 worktree 不影响缓存与发布数据。

### 提交信息
- 格式：`<分类>: <修改点描述>`，分类如 `feat` / `fix` / `refactor` / `docs` / `chore` 等
- 描述要点到修改层面即可，不要罗列具体代码行
- **禁止添加任何 `Co-Authored-By` 署名**（包括 Claude / Cursor / ChatGPT 等所有 AI 助手）

### Issue 规范

所有进入开发的事项必须以 GitHub issue 形式登记，字段缺失的 issue 不予开工：

- **背景**：为什么做这件事（业务 / 技术 / 风险驱动力）。一句话能说清的不要写两段。
- **问题 / 需求描述**：具体要解决什么。现状（现有代码、日志、数据）是什么，期望状态是什么。触及交易安全红线的必须明确标出。
- **相关 issue / PR**：列出前置、后继、重复、相关项，用 GitHub 关键字关联：`blocked by #N` / `blocks #N` / `related to #N` / `duplicate of #N`。
- **验收标准（Acceptance Criteria）**：以可勾选清单形式给出 pass/fail 条件，至少覆盖：
  - 功能点（映射到 `phase1_doc.md` §3.4 端到端验收步骤中相关的条目）
  - 测试要求（单元 / 集成 / 故障注入）
  - 文档变更（AGENTS.md / README / 接口契约）
  - 风险要求（是否触及交易安全红线、是否需要用户确认）

### PR 规范

PR 是变更进入 `dev` / 里程碑分支的唯一入口。PR 描述必须包含以下字段：

- **实现情况**：逐条对照 issue 验收标准说明完成情况；未完成项必须显式标出并说明原因，不能偷偷漏掉。
- **关键决策**：实现过程中做出的技术选型、取舍与理由。为什么 A 方案而不是 B 方案、跳过某项检查的原因、新引入的依赖、对外接口的变更。
- **Know-how / 坑位**：沉淀开发过程中的经验 —— 调试技巧、踩到的坑及根因、规避方式、参考链接。目标是让后续接手的人能快速复用、不重蹈覆辙。
- **测试与验证**：本地执行了哪些命令（lint / typecheck / test 的具体命令与用例），结果如何；CI 状态。
- **风险与回滚**：是否触及交易安全红线（`client_order_id` 唯一性 / 下单超时不重试 / 持仓真实来源 / 重启恢复 / Kill Switch）？触及的话是否已与用户确认？回滚方案（迁移 down / feature flag / 配置回退）是什么？

PR 标题遵循提交信息规范（`<分类>: <修改点描述>`），并在描述中用 `Closes #N` 让合并后自动关闭对应 issue。

## 常用工具
- `gh` — 创建 PR、issue、查看 CI 等 GitHub 操作的首选方式

## 记忆（Memory）规范

- **记忆文件统一存放在仓库 `docs/memory/` 下**，一篇记忆一个 Markdown 文件
  （`<kebab-case-slug>.md`），内容与格式不限但至少包含：主题、结论 / 事实、
  **Why（为什么）** 与 **How to apply（下次如何应用）**；相对日期一律转绝对日期。
- **`docs/memory/README.md` 是唯一索引**：每个记忆文件一行指针
  （`- [标题](文件.md) — 一句话钩子`），新增 / 修改 / 归档记忆时同步更新。
- **禁止把记忆写入任何 coding 框架自有的路径**（如各框架的 `~/.` 记忆目录、
  会话目录、私有缓存），保证记忆可在不同框架 / 工具间共享协作。
- 仓库中已能直接查到的事实（代码结构、git 历史、AGENTS.md 本身、issue/PR 描述）
  不重复记录；只记「不读代码 / 不翻历史就不知道」的经验与决策（踩坑根因、
  环境特殊性、用户偏好、跨 issue 的上下文）。
- 记忆属于项目资产，随 PR 进仓库（与代码 / 文档一样走评审与合并），不留在
  本地私有位置。

## 当前仓库状态

### 构建 / 测试 / lint / typecheck 命令

```bash
# 运行全部测试（需 PostgreSQL 运行在 127.0.0.1:5432）
uv run pytest tests/ -v

# 仅单元测试（不需 DB）
uv run pytest tests/unit/ -v

# 仅集成测试（需 DB）
uv run pytest tests/integration/ -v

# Lint
uv run ruff check packages/ tests/

# Type check
uv run mypy .

# DB 迁移
uv run alembic upgrade head
```

**pytest 临时目录（Windows ACL 根除，勿绕行）**：所有 pytest 调用统一使用仓库内
`.pytest-tmp/`（`pyproject.toml` addopts 已固定 `--basetemp .pytest-tmp` +
`-p no:cacheprovider`，该目录已入 `.gitignore`）。原因：`%LOCALAPPDATA%` 下默认
`pytest-of-<user>` 与 `.pytest_cache` 在本机偶发 WinError 5 拒绝访问（ACL 损坏），
导致 `tmp_path` 系 fixture 报错。因此**所有 pytest 必须在仓库根目录运行**（相对路径
以 CWD 为基准），不要手动加 `--basetemp` 指向系统临时目录，也不要为绕过报错而
关闭/改写该配置。若仓库根目录没有 `.pytest-tmp`，pytest 会自动创建；CI 与本地行为一致。

### Monorepo 结构

```
packages/
  finboard-shared/     — 领域模型 / 类型 / 异常 / ID 生成
  finboard-persistence/ — SQLAlchemy ORM / Repository / Alembic 迁移
  finboard-broker/      — BrokerAdapter / MarketDataAdapter 抽象 + Mock 实现 + factory
  finboard-broker-qmt/  — QMT (xtquant) 适配器 — 交易 + 行情(xtdata)（Windows-only）
  finboard-core/        — TradingKernel / OrderManager / PositionManager / 状态机 / EventBus
  finboard-risk/        — PreTradeChecker / KillSwitch / RiskConfig
  finboard-reconcile/   — ReconciliationEngine（只读核对）+ RecoveryEngine（状态修复）
  finboard-scheduler/   — asyncio 定时任务调度（盘前检查 / 收盘撤单 / 日终核对 / 连接心跳）
  finboard-app/         — 组装根 / CLI / 配置
  finboard-api/         — FastAPI REST + WebSocket API（人工交易控制台后端）
  finboard-mcp/         — FinBoard MCP Server（向 OpenCode 等外置 Agent 暴露受控研究工具，issue #108）
  finboard-opencode/    — OpenCode 研究运行时集成（会话关联/SSE 事件/中断恢复，issue #109）
```
