---
name: issue-187-joined-releases-factor-snapshot
description: issue #187 daily_metrics/financial_indicators 冻结发布与多数据集联合消费已实现(2026-08-18);market_cap 注册为 catalog 因子;联合发布快照需显式 volatility_windows
metadata:
  type: project
---

2026-08-18 issue #187(daily_metrics/financial_indicators 冻结发布与多数据集联合
消费)已实现,PR 待合并(目标分支 m/research-backtest)。核心:releases.py 的
`ReleaseDatasetKind`(bars/daily_metrics/financial_indicators)与字段白名单
(`DAILY_METRICS_FIELDS` / `FINANCIAL_INDICATORS_FIELDS`)、
`FrozenDatasetReleaseBuilder` 注入 `ResearchDataReleaseSource`(persistence 侧
`ResearchTableReleaseSource` 从 research_* 表读取)做非 bars 冻结,
`FrozenReleaseProvider.fetch_daily_metrics/financial_indicators` PIT 门控读取;
frozen_loader / signal_engine / strategy_specs / research_runs 的 `dataset_release_ids`
可引用多份发布但候选池始终落在 bars 主发布;factor_lab 新增
`build_cross_section_feature_snapshot_from_releases` 产出 pb/市值/换手/ROE 等。

**关键设计决策与坑(改前先想清楚):**
1. **`market_cap` 必须注册到 `FACTOR_LAB_CATALOG`**:`FeatureObservation.__post_init__`
   硬校验 feature_name 须 `get_factor_definition` 可查(KeyError 即抛),而 design 注释
   曾以为「纯 feature_id 无需注册」。修复:注册为 `Role.RISK` /
   `signal_eligible=False` / `unit="cny"` / `transform="raw"`(与 selection.py 的
   `FactorName.MARKET_CAP` 一致),不改 `FeatureObservation` 校验。注意
   `FACTOR_LAB_CATALOG` ≠ 策略编译的 `FACTOR_CATALOG`(compiler._factor_sources 用后者),
   注册不影响 strategy validate。
2. **联合发布快照测试必须显式传 `volatility_windows`**:`_add_price_factors` 守卫
   `len(prices) >= max(momentum_lookback, max(windows)) + 1`,默认 (20,60,120) 需
   121 个点,短区间测试(64 交易日)全 price 因子静默跳过(不报错,只缺观测)。测试传
   `momentum_lookback=5, volatility_windows=(20,)`。
3. **financial_indicators 的 report_period 必须在发布区间内**:`_quarter_end_dates`
   只算 start~end 内的季度末,集成测试 _END=03-29 时 report_period=03-31 超出范围被
   `_fetch_research_records` start/end 过滤 → records 空 KeyError。_END 取季度末。
4. **daily_metrics coverage 按交易日算**:expected_sessions = `_trading_days(start,end)`
   数(真实 A 股日历,非纯工作日 count),须写满全部交易日行否则
   `coverage:0.017<0.98` 质量门失败(单元测试的 stub source 内部已按范围过滤,
   集成测试要真插 64 行)。
5. **`research_*` 表有 `batch_id` 外键**:集成测试直接插 `ResearchDailyMetricModel` 行
   需先建 `ResearchSyncBatchModel`(dataset/source/dataset_version 唯一约束),
   `ResearchTableReleaseSource` 不查 batch,只按 symbol/日期范围。
6. **联通 REST 快照端点** `FeatureSnapshotCreate.additional_release_ids`:bars 主发布 +
   research 附加发布。research 发布要求 `source=tushare` + `adjustment=none` +
   `required_capabilities=("stock",)`(service `_require_a_share_stock_scope`)。
7. mypy:研究记录在冻结路径是领域对象、读取路径是 dict,`_fetch_research_records`
   返回须标 `list[dict[str, object]]`;`ResearchDataReleaseSource` 协议用
   `Sequence[str]`(实现/测试 stub 的 `list[str]` 会 arg-type 失配)。

**Why:** 因子快照此前只从 bars 产出价格因子,pb/市值/换手/ROE 等基本面因子没有
独立数据源解冻;#187 打通「daily_metrics+financial 联合发布 → 因子快照 → 规格引用」,让
multi_factor 策略能真正引用基本面因子。

**How to apply:** 加新 research 数据集 kind 看 releases.py 的 `_freeze_research_instrument`
与 `_audit_research_records`;观察 `extract_factor_matrix` 支持新 factor 时先在
`FACTOR_LAB_CATALOG` 注册(有 `signal_eligible`/unit 语义)再想消费端;测试的
日期区间与覆盖率口径要先核对质量门。测试:单元在
`tests/unit/data/test_research_release.py` + `tests/unit/factor_lab/test_release_research_snapshot.py`,
集成在 `tests/integration/test_research_release_joined.py`(#187)。
