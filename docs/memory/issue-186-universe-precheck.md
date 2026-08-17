---
name: issue-186-universe-precheck
description: issue #186 universe 预检与候选池诊断已实现(2026-08-18);静态预览只对确定性元数据判空,价格/特征字段不误报;enqueue 创建路径 updated_at 懒加载坑已修
metadata:
  type: project
---

2026-08-18 issue #186(universe 预检与候选池诊断)已实现,PR 待合并(目标分支
m/research-backtest)。核心:新建 `finboard_backtest/strategy_spec/universe_precheck.py`
纯函数(`preview_universe_pool` / `universe_filter_warnings` / `describe_empty_pool`),
`strategy_validate`(REST+MCP)返回体加 `universe_precheck` 字段,
`run_queue`(REST `queue_research_run` + MCP `_build_queued_manifest`)入队时对主发布
instruments 做候选池非空校验(空池秒级 invalid_argument,附排除统计+缺失字段),
signal_engine 执行期补降级 warning + 空池错误附根因字段名。

**关键设计决策(改前先想清楚):**
1. 静态预览**只对确定性元数据判空**——list_date/delist_date/suspended/coverage/
   present_event_types/市场/资产类别参与空池判定;price 与特征类字段(min_price、
   min_average_amount 等)运行时恒有(bars / 冻结快照 / 多期重算),缺失只发具名
   warning,绝不因 price 为 None 误报空池。`available_features` 默认含标准价格
   特征集 `STANDARD_PRICE_FEATURE_NAMES`(momentum/volatility_20d/60d/120d/
   downside_volatility),另加规格 feature_graph 的 source 与冻结快照观测特征名。
2. `average_amount` 很特殊:是 `_BUILTIN_FIELDS` 之一但取值来自特征观测,实际
   **无任何 producer 输出该特征**;multi_factor 模板的 `required_data_fields`
   不含它,但 etf_rotation/mean_reversion 模板 `ranking_field="average_amount"`
   —— 这类策略入队会因 missing_ranking_field 秒级失败(需自己加快照特征才能过)。
3. enqueue 创建路径有个**既有潜伏 bug 已顺带修复**:`queue_research_run` 成功
   后直接 `ResearchRunOut.model_validate(row)` 序列化新插入对象,但
   research_runs.created_at/updated_at 是 `server_default=func.now()`(onupdate 列
   无 RETURNING 保证),async 上下文同步访问触发 MissingGreenlet(500)。修复:
   commit 后 `ResearchRunRepository.get(run_id)` 重读整行再序列化(与 cancel/replay
   口径一致)。MCP `_run_summary` 不读 updated_at,故 MCP 侧不受影响。
4. `releases[0]`(manifest.dataset_releases 第一项)是 signal_engine 实际使用的
   发布,预检只对它做;决策日取 `release.end_date`(最宽松时点,空池判定无假阳性)。

**Why:** 背景是 OpenCode agent 实测:list_date 全 null → listing_days=0 → 全排除
→ 回测跑 30+ 分钟才报泛化错误。预检与秒级失败把根因前移;执行期错误也附根因。

**How to apply:** 新加 universe 过滤条件时同步三处:(1) `universe_precheck.py`
的条件→依赖字段映射与 `_metadata_warnings`;(2) signal_engine 运行时 warning 聚合;
(3) docs/memory 与 AGENTS.md 里程碑段。测试:单元在
`tests/unit/strategy_spec/test_universe_precheck.py`,集成在
`tests/integration/test_universe_precheck_enqueue.py`(REST 入队秒级失败 +
正对照)+ `test_research_run_signal_engine_worker.py`(执行期空池根因)。