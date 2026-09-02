# 数据基座运营手册(研究数据同步与主数据治理)

跨会话运营步骤的 canonical 事实源:研究 / 数据 agent 与主 coding agent 共享。
与 `research_memories` 记忆冲突时以本文档为准(#268 约定)。

## v1 选股(research_db)必需数据集的发布状态(#255)

**背景**:2026-09-01 runs 273-275(ROE / PB / 换手率 top-N)全部 0 交易
「成功」。根因:**摄取 ≠ 发布**——`research_data_sync` 会在质量门通过后自动
`mark_published`(批次 `status=published`),但 profiles 批次的**同步+发布**
步骤从未进入运行手册;`selection.inputs_mode=research_db`(默认)必需
`instrument_profiles` 批次已发布,未发布时旧链路逐期 SKIPPED、引擎 0 交易收场。

**已建的防线(#255)**:

- 入队期:REST `POST /api/backtest/run` 与 MCP `finboard_backtest_run`(同步 +
  异步入队)对 `enabled + inputs_mode=research_db` 的 selection 逐个检查
  `required_datasets` 的已发布批次,缺失秒级 422 / `invalid_argument`
  (`dataset_unpublished:{dataset}` 具名);
- 执行端:`run_backtest_and_persist`(worker,覆盖 grid 提交等旁路)重放同一
  检查,失败 `selection_dataset_unpublished` 具名错误,任务不重试;
- 引擎:选股启用的 run 一律随结果携带 `selection_diagnostics`
  (published/skipped 快照数、skip 原因计数、候选池是否曾生效),整期无候选
  时打 `backtest.selection_pool_never_active` warning 并在 `summary()` 标注
  「本 run 大概率 0 交易」。

**运营步骤(选股回测前的核验清单)**:

1. 核验批次发布状态(哪些 dataset、哪个 version、是否 published):

   ```sql
   SELECT dataset, source, dataset_version, status, published_at, accepted_rows
   FROM research_sync_batches
   WHERE source = 'tushare'
   ORDER BY dataset, id DESC;
   ```

   选股回测要求的 datasets 按 selection 配置推导(默认 research_db 至少含
   `instrument_profiles`;配置了市值/PB/换手过滤或排名再加 `daily_metrics`,
   ROE/毛利率/营收增速再加 `financial_indicators`)。

2. 缺失或 `status != 'published'` 时:`finboard_job_enqueue(kind=research_data_sync,
   payload={datasets: [...], symbols: [...], start_date: ..., end_date: ...})`
   摄取;质量门通过即自动发布(见任务 phase 摘要);质量门失败看批次
   `quality_report` 修复后重跑。

3. 若 selection 显式声明 `dataset_versions`,发布版本必须与之精确匹配
   (`published_version` 口径);通常不声明即可(取最近发布)。

4. 发布后入队仍有疑虑时:先跑 `finboard_strategy_validate`(research_run 侧
   universe 预检)或小规模同步 `finboard_backtest_run` 验证选股出单,再放大区间。

**边界**:粗粒度门只回答「是否存在已发布批次」;批次已发布但不覆盖具体
交易日(如 daily_metrics 只发到 2023)仍由逐期 SKIPPED 的 `skip_reason`
承载,配合 `selection_diagnostics.skip_reasons` 可见。

## instruments 主数据元数据(list_date / industry / delist_date / 名称历史,#251)

**背景**:`instruments` 主数据由 akshare 发现链路写入,只含
code/name/market/instrument_type/exchange/listing_board。list_date、industry、
delist_date 的结构化上游都在 tushare:

| 字段 | 上游 | 落点 | 回填路径 |
| --- | --- | --- | --- |
| list_date / industry | `stock_basic`(list_status=L) | `research_instrument_profiles` | data_sync 后置回填 |
| delist_date | `stock_basic`(list_status=D 退市档案) | 同上 | 同上 |
| 名称历史(PIT) | `namechange` | `instrument_names` 主数据表 | research_data_sync `name_changes` dataset 直接重建 |

**标准同步顺序**(每次刷新全市场主数据时):

1. `research_data_sync`(默认全部 datasets,或显式 `["profiles", "name_changes"]`)
   —— profiles 段同时拉取在市(L)与退市(D)档案合并进同一批次;
   name_changes 段全市场分页拉取名称变更并按半开区间重建 `instrument_names`
   (未提及 symbol 保留既有记录,重跑幂等)。
2. `dataset_release_publish`(release_kind=`instrument_profiles` 或联合发布)
   —— 发布 profiles 批次。发布构造器兜底(instruments 字段为 null 时从档案
   补齐)**仍要求已发布批次**;回填不要求(见下)。
3. `data_sync`(universe 全市场同步)—— 同步 `instruments` 并后置回填:
   从最近一次**实际摄取过档案的批次**(不论发布状态,#251 修复)回填
   list_date / industry / delist_date(只填 null,不覆盖)。
   `status` 不随回填变更 —— 退市状态由缺席二次确认(sync_with_diff)推进。

**验证 SQL**(回填效果):

```sql
-- 元数据非空率
SELECT count(*) FILTER (WHERE list_date IS NULL)  AS missing_list_date,
       count(*) FILTER (WHERE industry IS NULL)  AS missing_industry,
       count(*) FILTER (WHERE delist_date IS NULL) AS missing_delist_date,
       count(*) AS total
FROM instruments;

-- 名称历史覆盖(#213 ST-PIT 依赖)
SELECT count(DISTINCT instrument_code) AS covered FROM instrument_names;
```

缺失统计同时出现在 `data_sync` 任务的 phase 摘要与 bars 发布的
`quality_report.instrument_metadata`(missing_list_date / missing_industry /
missing_delist_date / with_name_history / name_history_coverage)。

**边界**:`sector` 无上游来源,保持 null;退市 `status` 变更只走缺席二次确认,
档案回填不触碰。

## 跨发布标的集一致性(#252)

**背景**:2026-09-01 实测全市场 `financial_indicators` 发布缺单只标的
002889.SZ(发布时点的 symbols 内联清单漏配),引用该发布的 multi_period
research_run 在执行期才发现并整条失败。三发布(bars / daily_metrics /
financial_indicators)的标的并集一致性必须自检。

**发布期校验**(推荐,秒级拦截):`dataset_release_publish` 发布研究发布时带
`consistency_baseline_release_id=<同区间 bars 主发布>` +
`consistency_fail_on_mismatch=true`——差集非空即拒绝发布(code=
`symbol_set_mismatch`,差集具名进错误 context);不带 fail 开关则只 warning。

**发布后自检**(任一入口,同一实现):

- MCP:`finboard_dataset_release_diff(release_id=<研究发布>,
  other_release_id=<bars 主发布>)` → `consistent` 布尔 + 差集具名清单;
- REST:`GET /api/instruments/datasets/releases/{id}/symbol-diff?other_release_id=...`。

**002889.SZ 补齐运营步骤**(历史缺口的修复路径):

1. `research_data_sync`(datasets 含 `financial_indicators`,symbols 含
   002889.SZ,报告期区间覆盖该股全部历史)补齐摄取;
2. 用全市场 symbols 清单重发布 `financial_indicators`(带
   `consistency_baseline_release_id` + `fail_on_mismatch=true`);
3. `finboard_dataset_release_diff` 复核 vs bars 主发布 → `consistent: true`;
4. 引用旧发布的 research_run 用新 release_id 重新入队(发布不可变,不回填)。

**执行期语义**(兜底):研究发布缺标的在 research_run 执行期**不再炸整条
run**——缺失标的的研究因子值为 null,发具名 `research_release_missing_symbols`
warning(release_id + 缺失清单),与 factor_lab #212 容忍语义一致;bars 主
发布缺标的仍 fail-closed。
