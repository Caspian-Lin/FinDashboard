# 数据基座运营手册(研究数据同步与主数据治理)

跨会话运营步骤的 canonical 事实源:研究 / 数据 agent 与主 coding agent 共享。
与 `research_memories` 记忆冲突时以本文档为准(#268 约定)。

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
