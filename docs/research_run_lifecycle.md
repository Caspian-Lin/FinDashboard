# 统一研究回测生命周期

`ResearchRun` 是 issue #80 引入的纯离线编排边界。它把冻结数据、因子快照、无代码
策略规格、组合与风险参数、研究撮合结果和绩效报告连接成一条可重放血缘:

```text
冻结数据 → 候选池 → 因子/特征 → 标准化信号 → 约束前目标仓位
        → 风险/组合约束 → 约束后目标仓位 → 离散调仓计划
        → 研究订单 → 研究成交 → 持仓/现金/盈亏 → 报告
```

该模块不导入 Broker、不连接账户、不复用实盘 `orders` / `fills` / `positions`
表。研究 run、指令、订单和成交 ID 必须使用 `RR-` 命名空间。策略不能直接修改
研究持仓;每个决策快照的数量必须能由之前快照和本期成交严格推导。

## 架构和职责

| 组件 | 职责 | 明确不做 |
|---|---|---|
| `ResearchRunManifest` | 冻结所有可复现输入并计算 checksum | 不接受源码或可执行引用 |
| 策略适配器 | 把 #29/#60-#64 的既有研究结果归一为 `DecisionBundle` | 不写数据库、不调用 Broker |
| `ResearchRunCoordinator` | 状态机、逐阶段血缘、恒等式、幂等、恢复、重放 | 不解释策略逻辑、不生成实盘订单 |
| `ResearchRunStore` | 存储端口;内存和 PostgreSQL 两种实现 | 不复用实盘 Repository |
| ResearchRun API | 排队、历史、artifact、血缘、取消、重放登记 | 没有 `/run` 或 `/execute` |

现有策略内部实现保持不变。统一适配器入口覆盖:

| `strategy_kind` | 来源 | 关键资产语义 |
|---|---|---|
| `ma_cross` | #29 / 通用 `BacktestEngine` | next-bar、A 股 T+1、涨跌停、停牌、手数和税费 |
| `multi_factor` | #60 因子框架 | PIT 因子快照、横截面选择、组合层目标权重 |
| `etf_rotation` | #61 | ETF 分类、T+0/T+1、资产大类上限 |
| `mean_reversion` | #62 | next-bar、状态过滤、持有期、冷却期 |
| `convertible_double_low` | #63 | 转债手数、强赎/回售/到期事件和费用 |
| `futures_tsmom` | #64 | 多空、合约乘数、保证金、结算和展期 |

资产规则仍由 #56 的研究撮合、#58 的 PIT 元数据/事件和各策略既有模拟器执行。
适配器必须把拒单、部分成交、未成交、现金/保证金不足、碎片持仓和展期结果原样
归一化;不能把未成交目标当成持仓。缺少所需数据能力时抛出
`UnsupportedResearchCapabilityError`,运行以 `rejected` 失败关闭。

## 冻结清单

`ResearchRunManifest` 至少冻结:

- 已发布 `ResearchStrategySpec` 全文及 checksum;
- 每个 `ResearchDatasetRelease` 的 ID、版本、checksum 和已就绪能力;
- 因子快照 ID、框架版本、checksum 和因子能力;
- 参数、验证、组合、风险、成交、费用和基准配置;
- `code_version`、10 万至 50 万元初始资金、请求主体;
- 重放来源 `replay_of_run_id`。

API 会从已发布策略规格补齐验证、组合、退出风控、成交费用和基准默认值，再保存
显式 overrides。依赖 FACTOR/RISK_FACTOR 输入的策略没有冻结因子快照时不能排队。
API 的 actor 只允许 `human`;领域层再次拒绝 `llm`。LLM 可以解释、比较或建议配置，
但不能触发运行。

## 状态机、checkpoint 和恢复

```text
queued ──→ running ──→ completed
  │           ├──────→ failed ───────┐
  │           ├──────→ interrupted ──┤──→ running
  │           └──────→ cancelled     │
  ├──────→ rejected                  │
  └──────→ cancelled                 └──→ cancelled
```

每个阶段 artifact 都独立 commit。进程启动恢复时，
`mark_stale_running_as_interrupted()` 将遗留 `running` 标记为 `interrupted`。
恢复执行必须用同一冻结清单从头确定性计算;已存在且 checksum 相同的 artifact 被
幂等跳过，内容不同则冲突失败。`artifact_id`、`run_id + sequence` 和
`run_id + trace_id` 均有数据库唯一约束，因此重启不能重复记成交或重复记账。

相同 `idempotency_key` 只允许对应同一 manifest。`replay()` 创建新的 `RR-` run，
但沿用全部冻结输入，并比较与源 run 无关的结果 checksum;不同即标记
`non_deterministic_replay`。

## 决策 artifact 和血缘

每个决策期按固定顺序写 10 个 artifact:

1. `universe`:候选标的、纳入/排除原因、市场和资产类别。
2. `features`:特征值、冻结来源和 `available_at`。
3. `signals`:标准化分数、动作、规则、理由和因子快照。
4. `targets_before_constraints`:信号映射的原始目标仓位。
5. `constraints`:每项风险/组合约束的前值、后值、上限和理由。
6. `targets_after_constraints`:最终目标仓位。
7. `rebalance_plan`:手数离散后的目标/当前/差量和预计金额。
8. `orders`:研究订单状态，包括拒绝原因。
9. `fills`:实际成交、费用、税、滑点和时间。
10. `ledger`:成交驱动持仓、现金、保证金、已实现/未实现盈亏和权益。

每个 artifact 有 `trace_id` 和 `parent_trace_ids`。从 fill artifact 调用血缘接口会
递归返回候选池到成交的完整上游链。报告是第 11 类 artifact，包含策略收益、正确
基准、现金、佣金、税、滑点、未成交缺口和约束影响。

强制恒等式包括:

- `cash + market_value == equity`，允许 0.01 元舍入误差;
- 账本持仓市值等于逐持仓市值之和;
- 账本已实现/未实现盈亏等于逐持仓归集;
- 成交必须引用同标的同方向研究订单，累计数量不得超过订单;
- 每个 fill ID 全 run 唯一;
- 当前持仓数量必须由历史成交动作推导，禁止策略乐观改仓。

## API 契约

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/research/runs` | 冻结输入并登记 `queued`;不执行 |
| `GET` | `/api/research/runs` | 按状态/策略列历史 |
| `GET` | `/api/research/runs/{run_id}` | manifest、状态、错误和报告 |
| `GET` | `/api/research/runs/{run_id}/artifacts` | 顺序读取全部阶段 |
| `GET` | `/api/research/runs/{run_id}/lineage/{trace_id}` | 读取上游血缘 |
| `POST` | `/api/research/runs/{run_id}/cancel` | 取消 queued/running/interrupted/failed |
| `POST` | `/api/research/runs/{run_id}/replay` | 复制冻结清单为新 queued run |

API 刻意没有同步执行端点。受控离线 worker/CLI 通过
`ResearchRunCoordinator.execute(manifest, registered_adapter)` 执行。网页只提交
结构化策略规格和参数，不接受 Python 策略代码。

主要错误码:

| 错误码 | 含义 | 状态 |
|---|---|---|
| `unsupported_capability` | 数据/事件/适配器能力不完整 | `rejected` |
| `interrupted` / `process_restart` | 可从 checkpoint 恢复 | `interrupted` |
| `non_deterministic_replay` | 相同冻结输入重放结果漂移 | `failed` |
| 异常类名 | 数据、策略、约束、记账或持久化异常 | `failed` |

## 从冻结数据到报告的复现检查

1. 用 `finboard data release` 创建不可变数据发布。
2. 用 `finboard data release-verify` 验证 manifest 和文件 SHA-256。
3. 创建并发布无代码策略规格;因子策略先发布 `FeatureSnapshot`。
4. `POST /api/research/runs` 冻结精确版本并排队。
5. 受控离线 worker 用注册适配器执行。
6. 用 run 详情、artifact 和 lineage API 审计报告。
7. 用 replay API 登记同版本重放，执行后比较 `result_checksum`。

仓库内的固定样本验证命令:

```bash
uv run pytest tests/unit/research_run tests/unit/test_api_research_runs.py -v
uv run pytest tests/integration/test_research_run_persistence.py -v
```

第一条覆盖六类策略统一入口、阶段契约、血缘、幂等、能力失败关闭、部分成交和
持仓/会计不变量。第二条用 PostgreSQL 覆盖完整运行、历史读取、重启恢复、
同版本重放和多策略隔离。

## 与 `phase1_doc.md` §3.4 的映射

研究环境不能声称完成真实券商验收。它只提供对应的离线等价证据:

- §3.4 3-9:研究调仓计划、订单、拒单/撤销/部分成交/成交 artifact;
- §3.4 10:成交驱动的研究持仓和盈亏;
- §3.4 11-12:checkpoint、`interrupted`、重启恢复和幂等回放;
- §3.4 13:反向成交动作关闭 long/short;
- §3.4 14:订单、成交、费用、现金、保证金、持仓和权益恒等式。

§3.4 1-2 的券商连接、真实账户资金和真实持仓不属于本模块。恢复实盘验证必须仍按
AGENTS.md 的 QMT 门槛和安全红线执行。

## 数据库与回滚

迁移 `d41e7b9c2a80` 只创建 `research_runs` 和 `research_run_artifacts`。两表没有
账户字段，也不引用任何实盘表。回滚可执行 `alembic downgrade c29d8e3f0a79`，
只会删除离线研究运行和 artifact 历史，不影响旧 `backtest_runs` 或实盘数据。
