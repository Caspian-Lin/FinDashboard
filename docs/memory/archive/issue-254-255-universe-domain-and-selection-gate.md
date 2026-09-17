# issue #254/#255 第二轮反馈收尾——universe 评估域收窄与 research_db 选股 fail-visible(2026-09-02)

## 主题

同会话完成 #254(universe 评估域与候选池边界)与 #255(research_db 选股
instrument_profiles 批次未发布静默 0 交易,runs 273-275)两个 P1 修复,
采用**叠分支**(255 分支从 254 分支切出)避免 AGENTS.md/Skill 文档冲突。

## 结论 / 事实

- **#254**:声明 `universe.explicit_symbols` 后,静态预检(universe_precheck)
  与运行时候选构建(signal_engine)共用新导出的 `explicit_symbol_domain[T]()`
  (PEP 695 泛型,保住 `ReleasedInstrument` 元素类型),评估域收窄为
  explicit ∩ 发布标的;`not_in_explicit_symbols` 全市场噪音统计从此消失
  (有意行为);`UniversePoolPreview` 新增 `explicit_total`/`explicit_missing`,
  **explicit 声明域按交集判空**(交集空 = 全部不可交易,入队秒级拒)。
- **#254 基准回退链**:显式标的 > 每期选股池动态等权
  (`metrics.equal_weight_selection_pool_return`,按 published 快照逐期取
  `selected_symbols` 等权日收益平均、空选股期持币持平)> 静态池等权 >
  首个标的;来源入 `BacktestResult.benchmark_source`(独立字段,**不要**
  塞进 `benchmark_config`——那是「用户配置了什么」的如实归档,#184 测试断言精确相等)。
- **#255**:三层防线共用 `ResearchDatasetRepository.selection_inputs_gate(config)`
  (粗粒度 = 是否存在已发布批次;`dataset_versions` 精确匹配):REST 入队 422 /
  MCP 同步+异步 `invalid_argument` / 执行端 `run_backtest_and_persist` 重放
  (`selection_dataset_unpublished`,覆盖 grid 旁路)。引擎选股启用一律携带
  `selection_diagnostics`(skip 原因计数、`zero_trading_suspected`),
  整期无候选打 `backtest.selection_pool_never_active` warning。
- **摄取 ≠ 发布**:`research_data_sync` 质量门通过即自动 `mark_published`,
  但 #212 只跑了摄取;v1 selection 的 `_resolve_factor_batch` 只认
  `status=published` 批次 → 逐期 SKIPPED → 引擎 0 交易「成功」。核验清单
  落在 `docs/research/data-ops.md`(#255 新增段)。

## Why

- 收窄逻辑放 `universe_precheck.py` 内部(而非各调用点):REST/MCP 四个
  `preview_universe_pool` 调用点零改动即全链路生效,静态/运行时保证一致。
- `benchmark_source` 与 `benchmark_config` 分字段:语义分别是「实际用了什么」
  vs「配置了什么」,合并会破坏 #184 归档契约。
- 门控放 repo 方法上(persistence 已依赖 finboard-data,可直接收
  `FactorSelectionConfig`),复用 `_resolve_batch` 原语;粗粒度不查
  trade_date 覆盖——日期缺口由逐期 SKIPPED + diagnostics 承载,避免入队期做重查询。

## How to apply(下次如何应用)

- 多 issue 同改 AGENTS.md/Skill 文档时,直接叠分支(第二个从第一个的 head 切),
  PR 描述注明依赖与合并顺序;比并行分支 + 事后 merge 里程碑干净。
- MCP 同步路径(直接 engine.run)与异步路径(`_enqueue_backtest_job`)是两条
  独立代码路径,加校验两处都要;grid.py 自建 job 落库不经
  `_enqueue_backtest_job`,只能靠执行端兜底。
- 给 `equal_weight_*` 类基准曲线写「空曲线」判定时,不能拿「equity 恒等
  initial」当条件(全持平是合法基准),要用「从未有成分生效日」类结构条件。
- 集成测试直接 `sa.insert(ResearchSyncBatchModel).values(...)` 造已发布批次
  比走整个 sync service 快得多;`research_sync_batches` 有
  (dataset, source, dataset_version) 唯一约束,测试间必须清表。

## 相关

- PR #275(#254)、PR(#255,叠于 #254 之上);AGENTS.md #184/#186/#218 段与
  新增 #255 段;`docs/research/data-ops.md`;Skill `references/tools.md`。
