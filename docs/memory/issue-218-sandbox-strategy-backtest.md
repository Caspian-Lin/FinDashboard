# issue #218 沙箱策略代码接入 multi_period 回测(2026-08-30 完成)

## 主题

L3 沙箱路线第四环(issue #218):agent 编写的**策略**代码经 strategy_spec
`code_artifact` 引用,由 research_run 逐决策日在一次性沙箱容器执行
`strategy.decide(ctx) -> targets`(目标权重),复用 #91 组合管线。
PR 从 `origin/m/research-backtest` 切出(`feat/sandbox-strategy-backtest-218`)。

## 结论 / 事实

- **协议 v1(kit 0.2.0)**:`StrategyContext` = PIT 数据视图 + **当前权重
  回显**(`current_weights`,引擎回显上一决策成交后的实际持仓市值占比,
  首轮为空)+ `StrategyConstraints` 只读视图 + params;`normalize_strategy_result`
  允许**空 targets**(= 全现金观望,与因子打分不同);harness `--mode strategy`
  产出 `targets.parquet`。镜像 tag 三处同步升 0.2.0(kit `__init__` /
  settings 默认 / Dockerfile ARG + README/Skill 文档)。
- **执行链**:`UserCodeStrategyAdapter`(research_run/user_code_engine.py)
  继承 `PortfolioPipelineAdapter`,与 multi_factor **共用**
  `build_decision_load_contexts`(从 `build_decision_inputs` 拆出的机械加载
  层:决策日推导/universe/价格/协方差同口径,报告同屏可比);每个决策日经
  `StrategySandboxCaller`(research_sandbox/strategy_exec.py,与 #216 共用
  runner/data_mount/静态校验/失败分类)跑一个容器(`--mode strategy`)。
  **不落 research_code_runs 行**(那是独立沙箱任务的记账单位),provenance
  由 research run report 的 `sandbox_provenance` 段归档(commit + 镜像
  digest + 逐决策 targets checksum)。
- **权重语义**:decide 输出 → NormalizedSignal(score=权重)→ 新增
  `DirectWeightsAllocator`(method=`direct_weights`):**不做 desired_gross
  重缩放**(权重和 < 1 = 持现金),equal_weight 兜底对该 method 禁用(会
  静默改写权重语义)。负权重/超上限**不在映射层截断**——交由组合管线
  约束投影逐项审计(单一截断权威);池外/缺执行元数据标的在映射层丢弃记
  warning。空目标决策走 build_portfolio 空分支,已补一条 cash 审计行
  (否则 research runner 的「缺少组合约束阶段」校验会拒绝合法的全现金决策)。
- **门控三道闸**:(1) 编译期 `compile_strategy_spec(user_code_sources=...)`;
  (2) 入队期 `user_code_reference_gate_error`(research_code/user_code.py,
  REST+MCP 共用:active/commit 一致/沙箱开关/single_shot 缺快照),放行时
  `freeze_user_code_commit` 把 active commit 冻结进 manifest(input_checksum
  覆盖代码版本,重放确定性);(3) 执行期数据全部来自冻结 manifest。
- **schema**:`StrategyCodeArtifactRef {name, commit?}`;`ResearchStrategySpec.
  code_artifact` 与 kind 双向互斥校验;**FeatureGraph/SignalRules 的
  min_length=1 从 Field 约束移到 spec 级交叉校验**(user_code 允许空,
  其余 kind 在 validator 里保持非空)——放宽 Field 约束不影响既有规格。
  `reject_executable_payload` 的 forbidden key 是**精确匹配**,`code_artifact`
  不踩 `code` 的雷。

## Why(为什么)

- 逐日决策函数(非事件驱动 on_bar)是用户 2026-08-28 的决策:纯研究域易
  审计、与 multi_period/#91 targets 管线天然对齐;引擎回显权重即可覆盖
  跨日路径依赖(动量持续/仓位爬坡/冷却)。
- targets 直接进管线而非转分数:转 equal_weight 分数会抹掉 decide 的权重
  信息;direct_weights 信任绝对值,#91 的约束/风险/资金/撮合机械全部复用,
  策略永远不触订单语义。
- 权重回显在服务端计算(而非容器间传递状态):策略保持纯截面函数,
  状态由引擎持有——重放确定性由「冻结 commit 的代码 + 每决策 PIT 挂载 +
  确定性管线」保证。

## How to apply(下次如何应用)

- 逐决策沙箱调用复用入口:`StrategySandboxCaller.create(settings, artifact_name,
  commit, release_provider_factory, dataset_release_ids, run_id, params)`
  —— **commit 必须由调用方冻结传入**(create 不解析 active,防 run 生命
  周期内 artifact 漂移)。
- 测试模式:集成测试 monkeypatch `user_code_engine.StrategySandboxCaller`
  + `UserCodeStrategyAdapter._ensure_loaded` 注入 fake;Docker E2E
  (FINBOARD_SANDBOX_E2E=1)用真实 git 仓库(`ResearchCodeService.submit`)
  + 真实容器 + InMemoryResearchRunStore,无需 DB。
- stub provider 双面形状:data_mount 吃 `.bar`(OHLCV 列,时间戳要
  tz-aware,否则价格特征链报「observed_at 必须带时区」),PIT 门控要按
  `decision_at.date()` 过滤(物理隔离防线会 fail-closed 拦截未来行);
  worker 模块的 `_StubBar(close, timestamp)` 无 symbol 字段,别与本地
  e2e 的 `_Bar(symbol=...)` 混用。
- 风险贡献上限(0.35)约束 decide 输出的持仓数:等权 n 只需 1/n < 0.35
  → n ≥ 3,fixture/样例要保证每月 ≥4 个正权重,否则 hard_constraint_rejected。
- InMemoryResearchRunStore 在 `finboard_backtest.research_run.store`(不在
  runner 模块);coordinator 的 `record.result` 是 `ResearchRunReport`
  dataclass(非 dict),属性访问。
